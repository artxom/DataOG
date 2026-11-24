"""Data.O.G 确定性 SQL / 存储过程静态解剖引擎、血缘 DAG 建模与语义保真压缩器.

核心设计哲学（纯 Python 标准库，0 Token 成本）：
1. 容错词法扫描：支持 `--` / `/* */` 注释、单引号/反斜杠转义、引用标识符及 PL/pgSQL `$$...$$` 过程体解包，精确映射 1-based 行号；
2. 语句级清单 (`statements`)：提取 `CREATE_PROC` / `CREATE_TEMP_TABLE` / `CTAS` / `INSERT` / `MERGE` / `UPDATE` / `DELETE` / `TRUNCATE` / `EXECUTE_IMMEDIATE` / `SELECT` / `CALL` 的起止行号、目标表、源表、CTE、循环标志与 Token 估算；
3. 数据血缘 DAG (`lineage_dag`)：自动归类 `source_only` / `temp_staging` / `final_target` 节点与跨语句数据流边，输出紧凑血缘链摘要；
4. 控制流与事务骨架 (`control_flow`)：提取参数/变量声明、游标与循环起止区间、动态 SQL、`COMMIT`/`ROLLBACK` 及 `EXCEPTION` 捕获点；
5. 确定性静态隐患雷达 (`static_findings`)：零幻觉定位 `LOOP_DML`、`UNBOUNDED_DML`、`DYNAMIC_SQL`、`MID_COMMIT`、`SELECT_STAR`、`DISTINCT_HEAVY`、`DIALECT_COMPAT`；
6. 语义保真压缩 (`compact_sql_for_llm`)：折叠超长静态 `CASE WHEN` 枚举映射与常量 `IN (...)` 列表、剥离装饰性横幅与长变更日志，严格保留原始 `Lxxx|` 真实行号。
"""

from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass
import math
import re
from typing import Any, Dict, List, Optional, Set, Tuple

_DOLLAR_TAG_RE = re.compile(r"^\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_BODY_SQL_HINT_RE = re.compile(
    r"(?i)\b(?:BEGIN|DECLARE|SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|TRUNCATE|LOOP|EXECUTE)\b"
)
_DECORATIVE_BANNER_RE = re.compile(r"^\s*(?:--+|/\*+|\*+/)\s*[=\-\*#~_+]{4,}\s*(?:\*/)?\s*$")
_SECTION_BANNER_RE = re.compile(
    r"^\s*--\s*(?:={2,}|-{3,}|#{1,4}|\*{2,})?\s*"
    r"(\[[^\]]{2,48}\][^\n]{0,48}|(?:Step|阶段|步骤|Part|第[一二三四五六七八九十0-9]+[步章节段]?|[0-9]+[\.、\)])\s*[^\n]{1,72})"
    r"\s*(?:={2,}|-{3,}|\*{2,})?\s*$",
    re.IGNORECASE,
)

_TABLE_STOPWORDS: Set[str] = {
    "SELECT",
    "WHERE",
    "SET",
    "VALUES",
    "VALUE",
    "DUAL",
    "LATERAL",
    "UNNEST",
    "TABLE",
    "ONLY",
    "AS",
    "ON",
    "AND",
    "OR",
    "NOT",
    "NULL",
    "INTO",
    "FROM",
    "JOIN",
    "LEFT",
    "RIGHT",
    "INNER",
    "OUTER",
    "FULL",
    "CROSS",
    "USING",
    "THEN",
    "ELSE",
    "ELSIF",
    "ELSEIF",
    "END",
    "CASE",
    "WHEN",
    "IF",
    "LOOP",
    "FOR",
    "WHILE",
    "BEGIN",
    "DECLARE",
    "RETURN",
    "CALL",
    "EXEC",
    "EXECUTE",
    "IMMEDIATE",
    "CURSOR",
    "OPEN",
    "FETCH",
    "CLOSE",
    "ROWCOUNT",
    "FOUND",
    "NOTFOUND",
    "EXISTS",
    "TEMP",
    "TEMPORARY",
    "GLOBAL",
    "LOCAL",
    "VOLATILE",
    "UNLOGGED",
    "REPLACE",
    "PROCEDURE",
    "FUNCTION",
    "VIEW",
    "INDEX",
    "TRIGGER",
    "PACKAGE",
    "BODY",
    "COMMIT",
    "ROLLBACK",
    "EXCEPTION",
    "RAISE",
    "PERFORM",
    "OVERWRITE",
    "PARTITION",
    "GENERATE_SERIES",
    "JSON_EACH",
    "JSONB_EACH",
    "JSON_ARRAY_ELEMENTS",
    "XMLTABLE",
    "NEW",
    "OLD",
}

_TEMP_NAME_RE = re.compile(r"(?i)(?:^#|^(?:tmp|temp|wrk|stg|stage)_|_(?:tmp|temp|wrk|stg)$)")

_WRITE_VERBS: Set[str] = {
    "CREATE_TEMP_TABLE",
    "CTAS",
    "CREATE_TABLE",
    "INSERT",
    "MERGE",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
}

_ORACLE_LEGACY_CHECKS: Tuple[Tuple[str, re.Pattern[str], str], ...] = (
    (
        "ORACLE_PLUS_JOIN",
        re.compile(r"\(\s*\+\s*\)"),
        "检测到 Oracle 遗留外连接写法 `(+)`，在 DWS/PGSQL/MySQL 中需改写为标准 `LEFT/RIGHT OUTER JOIN`",
    ),
    (
        "ORACLE_ROWNUM",
        re.compile(r"(?i)\bROWNUM\b"),
        "检测到 Oracle `ROWNUM` 伪列，在 DWS/PGSQL/MySQL 中建议改写为 `LIMIT` 或 `ROW_NUMBER() OVER (...)`",
    ),
    (
        "ORACLE_DECODE",
        re.compile(r"(?i)\bDECODE\s*\("),
        "检测到 Oracle `DECODE(...)` 函数，跨方言标准写法建议使用 `CASE WHEN ... THEN ... END`",
    ),
    (
        "ORACLE_NVL",
        re.compile(r"(?i)\bNVL2?\s*\("),
        "检测到 `NVL/NVL2(...)` 函数，标准 ANSI SQL / PGSQL / MySQL 建议改写为 `COALESCE(...)` / `CASE WHEN`",
    ),
    (
        "ORACLE_FROM_DUAL",
        re.compile(r"(?i)\bFROM\s+DUAL\b"),
        "检测到 `FROM DUAL` 伪表写法，PGSQL/SQLite/部分数仓可直接省略 `FROM DUAL`",
    ),
    (
        "ORACLE_SYSDATE",
        re.compile(r"(?i)\b(?:TRUNC\s*\(\s*SYSDATE\b|SYSDATE\b)"),
        "检测到 Oracle `SYSDATE` / `TRUNC(SYSDATE)`，在 PGSQL/DWS/MySQL 中建议确认是否需替换为 `CURRENT_TIMESTAMP` / `CURRENT_DATE` / `DATE_TRUNC`",
    ),
    (
        "ORACLE_CONNECT_BY",
        re.compile(r"(?i)\bCONNECT\s+BY\b"),
        "检测到 Oracle `CONNECT BY` 层次查询，在 PGSQL/DWS/MySQL 中通常需改写为 `WITH RECURSIVE`",
    ),
)

_ORACLE_NATIVE_DIALECTS: Set[str] = {"ORACLE", "DM8", "OB_ORACLE"}


def _est_tokens(text: str) -> int:
    """与 attachments.estimate_tokens 一致的 O(N) 保守 Token 估算."""
    if not text:
        return 0
    char_len = len(text)
    utf8_len = len(text.encode("utf-8", errors="ignore"))
    wide_approx = max(0, min(char_len, (utf8_len - char_len) // 2))
    ascii_approx = max(0, char_len - wide_approx)
    return max(1, math.ceil(wide_approx * 1.5 + ascii_approx * 0.28))


def _build_line_offsets(text: str) -> List[int]:
    offsets = [0]
    pos = text.find("\n")
    while pos != -1:
        offsets.append(pos + 1)
        pos = text.find("\n", pos + 1)
    return offsets


def _offset_to_line(offsets: List[int], char_pos: int) -> int:
    return bisect.bisect_right(offsets, max(0, char_pos))


@dataclass(frozen=True)
class _SqlToken:
    kind: str  # WORD | QIDENT | STRING | NUMBER | LP | RP | SEMI | COMMA | DOT | DOLLAR_DELIM | OP
    upper: str
    raw: str
    start: int
    end: int
    line: int


def _tokenize_sql_tolerant(sql: str, line_offsets: List[int]) -> Tuple[List[_SqlToken], List[Tuple[int, str]]]:
    """容错 SQL/PL-SQL 词法扫描，解包过程体 `$$...$$` 并记录章节注释锚点."""
    tokens: List[_SqlToken] = []
    section_comments: List[Tuple[int, str]] = []
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
            end_i = n if nl == -1 else nl
            raw_cmt = sql[i:end_i]
            ln = _offset_to_line(line_offsets, i)
            bm = _SECTION_BANNER_RE.match(raw_cmt)
            if bm:
                title = re.sub(r"^[\-\=\*#\s]+|[\-\=\*#\s]+$", "", bm.group(1)).strip()
                if len(title) >= 2:
                    section_comments.append((ln, title[:64]))
            i = n if nl == -1 else nl + 1
            continue

        # 块注释 /* ... */（容错未闭合）
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end_idx = sql.find("*/", i + 2)
            i = n if end_idx == -1 else end_idx + 2
            continue

        # Dollar-Quoting $tag$...$tag$
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql[i : i + 64])
            if m:
                tag = m.group(0)
                ln = _offset_to_line(line_offsets, i)
                close_pos = sql.find(tag, i + len(tag))
                if close_pos != -1:
                    inner = sql[i + len(tag) : close_pos]
                    if inner.count("\n") >= 2 or _BODY_SQL_HINT_RE.search(inner[:2000]):
                        # 过程体/函数体定界符：保留定界符 token 并继续深入扫描内部 SQL 语句
                        tokens.append(_SqlToken("DOLLAR_DELIM", tag.upper(), tag, i, i + len(tag), ln))
                        i += len(tag)
                        continue
                    end_pos = close_pos + len(tag)
                    tokens.append(_SqlToken("STRING", "'__DOLLAR__'", sql[i:end_pos], i, end_pos, ln))
                    i = end_pos
                    continue
                tokens.append(_SqlToken("DOLLAR_DELIM", tag.upper(), tag, i, i + len(tag), ln))
                i += len(tag)
                continue
            tokens.append(_SqlToken("OP", "$", "$", i, i + 1, _offset_to_line(line_offsets, i)))
            i += 1
            continue

        # 单引号字符串（兼容 E'...' / N'...' 与 '' 转义，容错未闭合）
        if ch == "'" or (
            ch in ("E", "e", "N", "n", "X", "x", "B", "b")
            and i + 1 < n
            and sql[i + 1] == "'"
            and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_"))
        ):
            ln = _offset_to_line(line_offsets, i)
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
            tokens.append(_SqlToken("STRING", "'__STR__'", sql[i:j], i, j, ln))
            i = j
            continue

        # 引用标识符 ("..." / `...` / [...])
        if ch in ('"', "`", "["):
            ln = _offset_to_line(line_offsets, i)
            close_ch = "]" if ch == "[" else ch
            j = i + 1
            chars: List[str] = []
            while j < n:
                c = sql[j]
                if c == close_ch:
                    if j + 1 < n and sql[j + 1] == close_ch:
                        chars.append(close_ch)
                        j += 2
                        continue
                    j += 1
                    break
                chars.append(c)
                j += 1
            inner = "".join(chars).strip()
            tokens.append(_SqlToken("QIDENT", inner.upper(), sql[i:j], i, j, ln))
            i = j
            continue

        ln = _offset_to_line(line_offsets, i)
        if ch == "(":
            tokens.append(_SqlToken("LP", "(", "(", i, i + 1, ln))
            i += 1
            continue
        if ch == ")":
            tokens.append(_SqlToken("RP", ")", ")", i, i + 1, ln))
            i += 1
            continue
        if ch == ";":
            tokens.append(_SqlToken("SEMI", ";", ";", i, i + 1, ln))
            i += 1
            continue
        if ch == ",":
            tokens.append(_SqlToken("COMMA", ",", ",", i, i + 1, ln))
            i += 1
            continue
        if ch == ".":
            tokens.append(_SqlToken("DOT", ".", ".", i, i + 1, ln))
            i += 1
            continue

        if ch.isdigit():
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in (".", "_")):
                j += 1
            raw_num = sql[i:j]
            tokens.append(_SqlToken("NUMBER", raw_num.upper(), raw_num, i, j, ln))
            i = j
            continue

        if ch.isalpha() or ch in ("_", "#", "@") or ord(ch) >= 128:
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in ("_", "$", "#") or ord(sql[j]) >= 128):
                j += 1
            raw_w = sql[i:j]
            tokens.append(_SqlToken("WORD", raw_w.upper(), raw_w, i, j, ln))
            i = j
            continue

        # 常见双字符运算符 (:=, ||, <>, !=, <=, >=)
        if i + 1 < n and sql[i : i + 2] in (":=", "||", "<>", "!=", "<=", ">="):
            op2 = sql[i : i + 2]
            tokens.append(_SqlToken("OP", op2, op2, i, i + 2, ln))
            i += 2
            continue

        tokens.append(_SqlToken("OP", ch, ch, i, i + 1, ln))
        i += 1

    return tokens, section_comments


def _clean_ident_part(tok: _SqlToken) -> str:
    if tok.kind == "QIDENT":
        return tok.raw.strip('"`[]').strip()
    return tok.raw.strip()


def _read_qualified_name(tokens: List[_SqlToken], idx: int) -> Tuple[str, int]:
    """从 idx 开始读取 `[db.][schema.]table` 限定名，返回 `(name, next_idx)`."""
    n = len(tokens)
    if idx >= n or tokens[idx].kind not in ("WORD", "QIDENT"):
        return "", idx
    first = tokens[idx]
    if first.kind == "WORD" and first.upper in _TABLE_STOPWORDS:
        return "", idx
    if first.raw.startswith((":", "@", "&", "$")):
        return "", idx
    parts = [_clean_ident_part(first)]
    j = idx + 1
    while j + 1 < n and tokens[j].kind == "DOT" and tokens[j + 1].kind in ("WORD", "QIDENT"):
        parts.append(_clean_ident_part(tokens[j + 1]))
        j += 2
        if len(parts) >= 3:
            break
    # 函数调用 `func(...)` 不是表名（除非前面明确是 INSERT INTO tbl(col1,col2) 等，调用方按场景区分）
    return ".".join(p for p in parts if p), j


def _extract_ctes(tokens: List[_SqlToken]) -> List[str]:
    """提取语句顶层 `WITH [RECURSIVE] cte1 AS (...), cte2 AS (...)` 中定义的 CTE 名称."""
    ctes: List[str] = []
    n = len(tokens)
    if not n or tokens[0].kind != "WORD" or tokens[0].upper != "WITH":
        return ctes
    idx = 1
    if idx < n and tokens[idx].kind == "WORD" and tokens[idx].upper == "RECURSIVE":
        idx += 1
    depth = 0
    while idx < n:
        if depth == 0:
            if tokens[idx].kind in ("WORD", "QIDENT"):
                cand = _clean_ident_part(tokens[idx])
                j = idx + 1
                # 可选列清单 cte(a, b) AS (...)
                if j < n and tokens[j].kind == "LP":
                    sub_d = 1
                    j += 1
                    while j < n and sub_d > 0:
                        if tokens[j].kind == "LP":
                            sub_d += 1
                        elif tokens[j].kind == "RP":
                            sub_d -= 1
                        j += 1
                if j < n and tokens[j].kind == "WORD" and tokens[j].upper == "AS":
                    j += 1
                    if j < n and tokens[j].kind == "WORD" and tokens[j].upper in ("NOT", "MATERIALIZED"):
                        j += 1
                        if j < n and tokens[j].kind == "WORD" and tokens[j].upper == "MATERIALIZED":
                            j += 1
                    if j < n and tokens[j].kind == "LP":
                        if cand and cand.upper() not in _TABLE_STOPWORDS:
                            ctes.append(cand)
                        sub_d = 1
                        j += 1
                        while j < n and sub_d > 0:
                            if tokens[j].kind == "LP":
                                sub_d += 1
                            elif tokens[j].kind == "RP":
                                sub_d -= 1
                            j += 1
                        if j < n and tokens[j].kind == "COMMA":
                            idx = j + 1
                            continue
                        break
            break
        idx += 1
    return ctes


def _extract_source_tables(tokens: List[_SqlToken], cte_names: Set[str]) -> List[str]:
    """扫描整个语句（含子查询/CTE）中的 `FROM` / `JOIN` / `USING` 源表."""
    sources: List[str] = []
    seen: Set[str] = set()
    n = len(tokens)
    i = 0

    def _add_table(name: str) -> None:
        if not name or len(name) < 2:
            return
        low = name.lower()
        if low in cte_names or name.upper() in _TABLE_STOPWORDS:
            return
        if low not in seen:
            seen.add(low)
            sources.append(name)

    while i < n:
        tok = tokens[i]
        if tok.kind == "WORD" and tok.upper in ("FROM", "JOIN", "USING"):
            # `USING (col1, col2)` 是 JOIN 列清单而非 MERGE USING 表
            if tok.upper == "USING" and i + 1 < n and tokens[i + 1].kind == "LP":
                i += 1
                continue
            # `EXTRACT(YEAR FROM col)` / `SUBSTRING(a FROM 1 FOR 3)` / `TRIM(BOTH ' ' FROM x)` 不是表
            if tok.upper == "FROM" and i >= 2:
                prev_up = tokens[i - 1].upper if tokens[i - 1].kind == "WORD" else ""
                if prev_up in ("YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "EPOCH", "BOTH", "LEADING", "TRAILING"):
                    i += 1
                    continue
            # `DELETE FROM target` 的直接目标表不算 source（但后续 USING/FROM 子查询仍会扫描）
            if tok.upper == "FROM" and i == 1 and tokens[0].kind == "WORD" and tokens[0].upper == "DELETE":
                i += 1
                continue

            j = i + 1
            if j < n and tokens[j].kind == "WORD" and tokens[j].upper == "ONLY":
                j += 1
            if j < n and tokens[j].kind == "LP":
                i += 1
                continue
            tbl, next_j = _read_qualified_name(tokens, j)
            # 跳过表函数调用如 `FROM unnest(...)` / `FROM table(...)`
            if tbl and not (next_j < n and tokens[next_j].kind == "LP"):
                _add_table(tbl)
                # 处理 ANSI 逗号连接 `FROM t1 a, t2 b WHERE ...`
                if tok.upper == "FROM":
                    k = next_j
                    while k < n:
                        tk = tokens[k]
                        if tk.kind == "WORD" and tk.upper in (
                            "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "INTERSECT",
                            "EXCEPT", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS",
                            "ON", "SET", "INTO", "RETURNING", "FOR", "WINDOW", "QUALIFY",
                        ):
                            break
                        if tk.kind in ("RP", "SEMI"):
                            break
                        if tk.kind == "COMMA":
                            c_tbl, c_next = _read_qualified_name(tokens, k + 1)
                            if c_tbl and not (c_next < n and tokens[c_next].kind == "LP"):
                                _add_table(c_tbl)
                                k = c_next
                                continue
                        k += 1
                i = next_j
                continue
        i += 1
    return sources


def _has_top_level_where(tokens: List[_SqlToken]) -> bool:
    depth = 0
    for tok in tokens:
        if tok.kind == "LP":
            depth += 1
        elif tok.kind == "RP":
            depth = max(0, depth - 1)
        elif depth == 0 and tok.kind == "WORD" and tok.upper == "WHERE":
            return True
    return False


def _has_select_star(tokens: List[_SqlToken]) -> bool:
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.kind == "WORD" and tok.upper == "SELECT":
            # 扫描到对应 FROM 前是否存在独立 `*` 或 `alias.*`（排除 COUNT(*)）
            depth = 0
            for j in range(i + 1, min(n, i + 40)):
                t = tokens[j]
                if t.kind == "LP":
                    depth += 1
                elif t.kind == "RP":
                    depth = max(0, depth - 1)
                elif depth == 0:
                    if t.kind == "WORD" and t.upper in ("FROM", "INTO", "WHERE"):
                        break
                    if t.kind == "OP" and t.raw == "*":
                        return True
    return False


def _has_distinct_heavy(tokens: List[_SqlToken]) -> bool:
    has_distinct = any(t.kind == "WORD" and t.upper == "DISTINCT" for t in tokens)
    if not has_distinct:
        return False
    has_join_or_wide = any(t.kind == "WORD" and t.upper in ("JOIN", "ORDER") for t in tokens)
    return has_join_or_wide


def _split_top_level_clauses(
    tokens: List[_SqlToken],
) -> List[List[_SqlToken]]:
    """按顶层 `;` 切分，并在 PL/SQL / PL/pgSQL 过程体内自动剥离块头/控制流前缀，拆分出独立内部语句."""
    raw_chunks: List[List[_SqlToken]] = []
    cur: List[_SqlToken] = []
    depth = 0
    for tok in tokens:
        if tok.kind == "DOLLAR_DELIM":
            if cur:
                raw_chunks.append(cur)
                cur = []
            continue
        if tok.kind == "LP":
            depth += 1
            cur.append(tok)
        elif tok.kind == "RP":
            depth = max(0, depth - 1)
            cur.append(tok)
        elif tok.kind == "SEMI" and depth == 0:
            if cur:
                raw_chunks.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        raw_chunks.append(cur)

    # 在块内部，`CREATE PROCEDURE ... AS/IS DECLARE ... BEGIN INSERT ...;` 可能不带分号直接连到 DECLARE/BEGIN
    # 以及 `FOR ... LOOP INSERT ...;` / `IF ... THEN UPDATE ...;` / `ELSE DELETE ...;` / `EXCEPTION WHEN ... THEN ...;`
    expanded: List[List[_SqlToken]] = []

    def _split_chunk_recursive(chunk: List[_SqlToken]) -> None:
        if not chunk:
            return
        n = len(chunk)
        # 1. CREATE [OR REPLACE] PROCEDURE/FUNCTION/PACKAGE/TRIGGER 头与后续 DECLARE/BEGIN 拆开
        if chunk[0].kind == "WORD" and chunk[0].upper == "CREATE":
            for k in range(1, min(n, 8)):
                if chunk[k].kind == "WORD" and chunk[k].upper in ("PROCEDURE", "FUNCTION", "PACKAGE", "TRIGGER"):
                    # 找到紧随签名之后的 AS/IS 后若跟 DECLARE 或 BEGIN
                    d = 0
                    for m in range(k + 1, n):
                        if chunk[m].kind == "LP":
                            d += 1
                        elif chunk[m].kind == "RP":
                            d = max(0, d - 1)
                        elif d == 0 and chunk[m].kind == "WORD" and chunk[m].upper in ("DECLARE", "BEGIN"):
                            expanded.append(chunk[:m])
                            _split_chunk_recursive(chunk[m:])
                            return
                    expanded.append(chunk)
                    return

        # 2. 过程体前导控制关键词（DECLARE / BEGIN / ELSE / EXCEPTION / LOOP）与内部 SQL 拆开
        head_u = chunk[0].upper if chunk[0].kind == "WORD" else ""
        if head_u in ("DECLARE", "BEGIN", "ELSE", "EXCEPTION"):
            expanded.append([chunk[0]])
            if n > 1:
                _split_chunk_recursive(chunk[1:])
            return

        # 3. `FOR ... LOOP <stmt>` / `WHILE ... LOOP <stmt>` / `LOOP <stmt>`
        if head_u in ("FOR", "WHILE", "LOOP"):
            if head_u == "LOOP":
                expanded.append([chunk[0]])
                if n > 1:
                    _split_chunk_recursive(chunk[1:])
                return
            d = 0
            for m in range(1, n):
                if chunk[m].kind == "LP":
                    d += 1
                elif chunk[m].kind == "RP":
                    d = max(0, d - 1)
                elif d == 0 and chunk[m].kind == "WORD" and chunk[m].upper == "LOOP":
                    expanded.append(chunk[: m + 1])
                    if m + 1 < n:
                        _split_chunk_recursive(chunk[m + 1 :])
                    return

        # 4. `IF ... THEN <stmt>` / `ELSIF ... THEN <stmt>` / `WHEN ... THEN <stmt>`
        if head_u in ("IF", "ELSIF", "ELSEIF", "WHEN"):
            d = 0
            for m in range(1, n):
                if chunk[m].kind == "LP":
                    d += 1
                elif chunk[m].kind == "RP":
                    d = max(0, d - 1)
                elif d == 0 and chunk[m].kind == "WORD" and chunk[m].upper == "THEN":
                    expanded.append(chunk[: m + 1])
                    if m + 1 < n:
                        _split_chunk_recursive(chunk[m + 1 :])
                    return

        expanded.append(chunk)

    for ch in raw_chunks:
        _split_chunk_recursive(ch)
    return expanded


def _classify_statement(
    stmt_tokens: List[_SqlToken],
    raw_sql: str,
) -> Tuple[str, str, List[str], List[str], bool, bool]:
    """分类语句并返回 `(verb, target_table, source_tables, ctes, is_temp, has_where)`."""
    if not stmt_tokens:
        return "OTHER", "", [], [], False, True

    first = stmt_tokens[0]
    w0 = first.upper if first.kind == "WORD" else ""
    n = len(stmt_tokens)

    ctes = _extract_ctes(stmt_tokens)
    cte_set = {c.lower() for c in ctes}
    sources = _extract_source_tables(stmt_tokens, cte_set)
    has_where = _has_top_level_where(stmt_tokens)

    # WITH cte AS (...) INSERT / UPDATE / DELETE / MERGE / SELECT
    eff_idx = 0
    if w0 == "WITH":
        # 跳过 CTE 定义寻找主动词
        d = 0
        for idx in range(1, n):
            tk = stmt_tokens[idx]
            if tk.kind == "LP":
                d += 1
            elif tk.kind == "RP":
                d = max(0, d - 1)
            elif d == 0 and tk.kind == "WORD" and tk.upper in ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"):
                eff_idx = idx
                w0 = tk.upper
                break

    if w0 == "CREATE":
        is_temp = False
        obj_kind = ""
        obj_idx = -1
        for k in range(eff_idx + 1, min(n, eff_idx + 10)):
            tk = stmt_tokens[k]
            if tk.kind != "WORD":
                continue
            up = tk.upper
            if up in ("TEMP", "TEMPORARY", "VOLATILE"):
                is_temp = True
            elif up in ("PROCEDURE", "FUNCTION", "PACKAGE", "TRIGGER"):
                obj_kind = up
                obj_idx = k + 1
                if up == "PACKAGE" and obj_idx < n and stmt_tokens[obj_idx].upper == "BODY":
                    obj_idx += 1
                break
            elif up == "TABLE":
                obj_kind = "TABLE"
                obj_idx = k + 1
                break
            elif up == "VIEW":
                obj_kind = "VIEW"
                obj_idx = k + 1
                break
            elif up == "INDEX":
                return "CREATE_INDEX", "", sources, ctes, False, True

        if obj_kind in ("PROCEDURE", "FUNCTION", "PACKAGE", "TRIGGER"):
            name, _ = _read_qualified_name(stmt_tokens, obj_idx)
            return "CREATE_PROC", name, [], [], False, True

        if obj_kind == "TABLE":
            # 跳过 IF NOT EXISTS
            j = obj_idx
            while j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper in ("IF", "NOT", "EXISTS"):
                j += 1
            tbl, after_tbl = _read_qualified_name(stmt_tokens, j)
            if tbl and _TEMP_NAME_RE.search(tbl.split(".")[-1]):
                is_temp = True
            # 判断是否为 CTAS (`AS SELECT` / `AS WITH`)
            has_as_select = any(
                stmt_tokens[m].kind == "WORD" and stmt_tokens[m].upper in ("SELECT", "WITH")
                for m in range(after_tbl, min(n, after_tbl + 24))
            )
            verb = "CREATE_TEMP_TABLE" if is_temp else ("CTAS" if has_as_select else "CREATE_TABLE")
            return verb, tbl, [s for s in sources if s.lower() != tbl.lower()], ctes, is_temp, True

        if obj_kind == "VIEW":
            j = obj_idx
            while j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper in ("IF", "NOT", "EXISTS"):
                j += 1
            tbl, _ = _read_qualified_name(stmt_tokens, j)
            return "CREATE_VIEW", tbl, [s for s in sources if s.lower() != tbl.lower()], ctes, False, True

        return "DDL", "", sources, ctes, False, True

    if w0 == "INSERT":
        j = eff_idx + 1
        while j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper in (
            "INTO", "OVERWRITE", "TABLE", "IGNORE", "OR", "REPLACE", "ALL", "FIRST",
        ):
            j += 1
        tbl, _ = _read_qualified_name(stmt_tokens, j)
        is_temp = bool(tbl and _TEMP_NAME_RE.search(tbl.split(".")[-1]))
        return "INSERT", tbl, [s for s in sources if s.lower() != tbl.lower()], ctes, is_temp, True

    if w0 == "MERGE":
        j = eff_idx + 1
        if j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper == "INTO":
            j += 1
        tbl, _ = _read_qualified_name(stmt_tokens, j)
        is_temp = bool(tbl and _TEMP_NAME_RE.search(tbl.split(".")[-1]))
        return "MERGE", tbl, [s for s in sources if s.lower() != tbl.lower()], ctes, is_temp, True

    if w0 == "UPDATE":
        j = eff_idx + 1
        if j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper == "ONLY":
            j += 1
        tbl, _ = _read_qualified_name(stmt_tokens, j)
        is_temp = bool(tbl and _TEMP_NAME_RE.search(tbl.split(".")[-1]))
        return "UPDATE", tbl, [s for s in sources if s.lower() != tbl.lower()], ctes, is_temp, has_where

    if w0 == "DELETE":
        j = eff_idx + 1
        if j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper == "FROM":
            j += 1
        if j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper == "ONLY":
            j += 1
        tbl, _ = _read_qualified_name(stmt_tokens, j)
        is_temp = bool(tbl and _TEMP_NAME_RE.search(tbl.split(".")[-1]))
        return "DELETE", tbl, [s for s in sources if s.lower() != tbl.lower()], ctes, is_temp, has_where

    if w0 == "TRUNCATE":
        j = eff_idx + 1
        if j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper == "TABLE":
            j += 1
        tbl, _ = _read_qualified_name(stmt_tokens, j)
        is_temp = bool(tbl and _TEMP_NAME_RE.search(tbl.split(".")[-1]))
        return "TRUNCATE", tbl, [], ctes, is_temp, True

    if w0 == "DROP":
        j = eff_idx + 1
        obj_t = ""
        if j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper in ("TEMP", "TEMPORARY"):
            j += 1
        if j < n and stmt_tokens[j].kind == "WORD":
            obj_t = stmt_tokens[j].upper
            j += 1
        while j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper in ("IF", "EXISTS"):
            j += 1
        tbl, _ = _read_qualified_name(stmt_tokens, j)
        if obj_t == "TABLE":
            return "DROP", tbl, [], ctes, bool(tbl and _TEMP_NAME_RE.search(tbl.split(".")[-1])), True
        return "DROP", tbl, [], ctes, False, True

    if w0 == "ALTER":
        j = eff_idx + 1
        if j < n and stmt_tokens[j].kind == "WORD" and stmt_tokens[j].upper == "TABLE":
            j += 1
        tbl, _ = _read_qualified_name(stmt_tokens, j)
        return "ALTER", tbl, [], ctes, False, True

    if w0 == "EXECUTE":
        if n > 1 and stmt_tokens[1].kind == "WORD" and stmt_tokens[1].upper == "IMMEDIATE":
            return "EXECUTE_IMMEDIATE", "", sources, ctes, False, True
        # PL/pgSQL `EXECUTE v_sql` / `EXECUTE 'INSERT ...'`
        if n > 1 and (
            stmt_tokens[1].kind == "STRING"
            or (stmt_tokens[1].kind == "WORD" and stmt_tokens[1].upper not in ("PROCEDURE", "FUNCTION"))
        ):
            return "EXECUTE_IMMEDIATE", "", sources, ctes, False, True
        return "CALL", "", sources, ctes, False, True

    if w0 in ("CALL", "EXEC", "PERFORM"):
        callee, _ = _read_qualified_name(stmt_tokens, eff_idx + 1)
        return "CALL", callee, sources, ctes, False, True

    if w0 == "SELECT":
        # 检测 `SELECT ... INTO tbl`（非标隐式建表或 PL/SQL 变量赋值）
        target_into = ""
        for k in range(eff_idx + 1, n):
            if stmt_tokens[k].kind == "WORD" and stmt_tokens[k].upper == "FROM":
                break
            if stmt_tokens[k].kind == "WORD" and stmt_tokens[k].upper == "INTO":
                cand, _ = _read_qualified_name(stmt_tokens, k + 1)
                if cand:
                    target_into = cand
                break
        return "SELECT", target_into, sources, ctes, False, True

    if w0 in ("COMMIT", "ROLLBACK"):
        return w0, "", [], [], False, True

    return "OTHER", "", sources, ctes, False, True


def analyze_sql_static(text: str, *, dialect: str = "") -> Dict[str, Any]:
    """对 SQL / 存储过程源码执行零 Token 成本确定性静态解剖，返回紧凑结构化档案."""
    if not text or not text.strip():
        return {
            "version": "1.0",
            "dialect": (dialect or "").strip().upper(),
            "line_count": 0,
            "total_statement_count": 0,
            "statements": [],
            "lineage_dag": {"nodes": [], "edges": [], "summary_lines": []},
            "control_flow": {
                "parameters": [],
                "variables": [],
                "cursors": [],
                "loops": [],
                "dynamic_sql": [],
                "transactions": [],
                "exceptions": [],
            },
            "static_findings": [],
            "finding_counts": {"HIGH": 0, "MEDIUM": 0, "LOW": 0},
        }

    eff_dialect = (dialect or "").strip().upper()
    line_offsets = _build_line_offsets(text)
    total_lines = len(text.splitlines())
    tokens, section_comments = _tokenize_sql_tolerant(text, line_offsets)
    clauses = _split_top_level_clauses(tokens)

    sec_lines = [ln for ln, _ in section_comments]

    def _section_at_line(ln: int) -> str:
        idx = bisect.bisect_right(sec_lines, ln) - 1
        return section_comments[idx][1] if idx >= 0 else ""

    # 控制流跟踪状态
    in_declare = False
    has_proc_block = False
    parameters: List[Dict[str, Any]] = []
    variables: List[Dict[str, Any]] = []
    cursors: List[Dict[str, Any]] = []
    loop_stack: List[Dict[str, Any]] = []
    loops: List[Dict[str, Any]] = []
    dynamic_sql_list: List[Dict[str, Any]] = []
    transactions: List[Dict[str, Any]] = []
    exceptions: List[Dict[str, Any]] = []

    raw_statements: List[Dict[str, Any]] = []
    static_findings: List[Dict[str, Any]] = []

    def _add_finding(
        code: str,
        severity: str,
        line: int,
        end_line: int,
        title: str,
        detail: str,
        *,
        stmt_id: str = "",
        table: str = "",
    ) -> None:
        fid = f"F{len(static_findings) + 1:03d}"
        static_findings.append(
            {
                "finding_id": fid,
                "code": code,
                "severity": severity,
                "stmt_id": stmt_id,
                "line": line,
                "end_line": end_line,
                "table": table,
                "title": title,
                "detail": detail[:240],
            }
        )

    for clause in clauses:
        if not clause:
            continue
        w0 = clause[0].upper if clause[0].kind == "WORD" else ""
        w1 = clause[1].upper if len(clause) > 1 and clause[1].kind == "WORD" else ""
        c_start_line = clause[0].line
        c_end_line = clause[-1].line
        c_snippet = re.sub(r"\s+", " ", text[clause[0].start : clause[-1].end]).strip()

        # 块状态切换
        if w0 == "DECLARE":
            in_declare = True
            has_proc_block = True
            # T-SQL / MySQL 单句 DECLARE v_x TYPE ...
            if len(clause) >= 3 and clause[1].kind in ("WORD", "QIDENT"):
                v_name = _clean_ident_part(clause[1])
                v_type = _clean_ident_part(clause[2]) if len(clause) > 2 else ""
                if v_name and len(variables) < 40:
                    variables.append({"name": v_name, "type": v_type, "line": c_start_line})
            continue

        if w0 == "BEGIN":
            in_declare = False
            has_proc_block = True
            continue

        if w0 == "EXCEPTION":
            in_declare = False
            has_proc_block = True
            exceptions.append({"line": c_start_line, "condition": "EXCEPTION_BLOCK"})
            continue

        if w0 == "WHEN" and len(clause) >= 2 and clause[-1].upper == "THEN":
            cond = " ".join(t.raw for t in clause[1:-1])[:64]
            if cond and len(exceptions) < 30:
                exceptions.append({"line": c_start_line, "condition": cond})
            continue

        # 游标声明 `CURSOR c_xxx IS|FOR ...` 或 `c_xxx CURSOR FOR ...`
        if w0 == "CURSOR" or (len(clause) >= 2 and w1 == "CURSOR"):
            cname = _clean_ident_part(clause[1] if w0 == "CURSOR" and len(clause) > 1 else clause[0])
            if cname and len(cursors) < 30:
                cursors.append({"name": cname, "line": c_start_line, "preview": c_snippet[:120]})
            # 游标内部查询也记录源表以便血缘追踪
            if any(t.kind == "WORD" and t.upper == "SELECT" for t in clause):
                sel_idx = next(i for i, t in enumerate(clause) if t.kind == "WORD" and t.upper == "SELECT")
                _, _, c_srcs, c_ctes, _, _ = _classify_statement(clause[sel_idx:], text)
                if c_srcs:
                    raw_statements.append(
                        {
                            "verb": "CURSOR_SELECT",
                            "start_line": c_start_line,
                            "end_line": c_end_line,
                            "target_table": "",
                            "source_tables": c_srcs,
                            "ctes": c_ctes,
                            "in_loop": bool(loop_stack),
                            "is_temp_target": False,
                            "has_where": True,
                            "estimated_tokens": _est_tokens(text[clause[0].start : clause[-1].end]),
                            "section_title": _section_at_line(c_start_line),
                            "preview": c_snippet[:100],
                            "raw_span": (clause[0].start, clause[-1].end),
                        }
                    )
            continue

        # 循环入栈与出栈
        if w0 in ("FOR", "WHILE", "LOOP") and clause[-1].kind == "WORD" and clause[-1].upper == "LOOP":
            kind = "FOR_LOOP" if w0 == "FOR" else ("WHILE_LOOP" if w0 == "WHILE" else "LOOP")
            loop_stack.append(
                {
                    "kind": kind,
                    "start_line": c_start_line,
                    "end_line": c_end_line,
                    "header": c_snippet[:96],
                }
            )
            has_proc_block = True
            # 若 FOR rec IN (SELECT ...) LOOP 含查询，提取源表
            if any(t.kind == "WORD" and t.upper == "SELECT" for t in clause):
                sel_idx = next(i for i, t in enumerate(clause) if t.kind == "WORD" and t.upper == "SELECT")
                _, _, l_srcs, l_ctes, _, _ = _classify_statement(clause[sel_idx:], text)
                if l_srcs:
                    raw_statements.append(
                        {
                            "verb": "SELECT",
                            "start_line": c_start_line,
                            "end_line": c_end_line,
                            "target_table": "",
                            "source_tables": l_srcs,
                            "ctes": l_ctes,
                            "in_loop": True,
                            "is_temp_target": False,
                            "has_where": True,
                            "estimated_tokens": _est_tokens(text[clause[0].start : clause[-1].end]),
                            "section_title": _section_at_line(c_start_line),
                            "preview": c_snippet[:100],
                            "raw_span": (clause[0].start, clause[-1].end),
                        }
                    )
            continue

        if w0 == "END" and w1 == "LOOP":
            if loop_stack:
                lp = loop_stack.pop()
                lp["end_line"] = c_end_line
                loops.append(lp)
            continue

        if w0 in ("END", "IF", "ELSIF", "ELSEIF", "ELSE", "OPEN", "FETCH", "CLOSE", "EXIT", "CONTINUE", "RETURN", "RAISE", "LANGUAGE"):
            continue

        # DECLARE 块内变量声明（非 SQL 语句）
        if in_declare and w0 not in ("CREATE", "SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "WITH", "TRUNCATE"):
            if len(clause) >= 2 and clause[0].kind in ("WORD", "QIDENT") and clause[1].kind in ("WORD", "QIDENT"):
                v_name = _clean_ident_part(clause[0])
                v_type = _clean_ident_part(clause[1])
                if v_name and v_name.upper() not in _TABLE_STOPWORDS and len(variables) < 40:
                    variables.append({"name": v_name, "type": v_type, "line": c_start_line})
            continue

        verb, target_tbl, src_tbls, ctes, is_temp, has_where = _classify_statement(clause, text)

        if verb == "CREATE_PROC":
            has_proc_block = True
            # 提取过程参数签名 (...)
            lp_i = next((k for k, t in enumerate(clause) if t.kind == "LP"), -1)
            if lp_i != -1:
                d = 1
                rp_i = lp_i + 1
                while rp_i < len(clause) and d > 0:
                    if clause[rp_i].kind == "LP":
                        d += 1
                    elif clause[rp_i].kind == "RP":
                        d -= 1
                    rp_i += 1
                param_text = text[clause[lp_i].end : clause[rp_i - 1].start].strip()
                if param_text:
                    for raw_p in param_text.split(",")[:20]:
                        pt = re.sub(r"\s+", " ", raw_p).strip()
                        if pt:
                            parameters.append({"spec": pt[:80], "line": c_start_line})

        if verb in ("COMMIT", "ROLLBACK"):
            in_lp = bool(loop_stack)
            transactions.append({"verb": verb, "line": c_start_line, "in_loop": in_lp})
            if has_proc_block:
                # 过程体内最后 5 行之前的 COMMIT/ROLLBACK 或循环内 COMMIT 属于中途提交风险
                if in_lp or c_start_line < max(1, total_lines - 6):
                    sev = "HIGH" if in_lp else "MEDIUM"
                    _add_finding(
                        "MID_COMMIT",
                        sev,
                        c_start_line,
                        c_end_line,
                        f"{'循环内' if in_lp else '过程中途'}出现 {verb}",
                        f"第 L{c_start_line} 行在{'循环体内部' if in_lp else '过程主干中途'}执行 {verb}，可能破坏事务原子性或在 DWS/PG 函数/子事务中触发兼容性报错。",
                    )
            continue

        if verb == "OTHER":
            continue

        stmt_raw = text[clause[0].start : clause[-1].end]
        in_lp = bool(loop_stack)
        if verb == "EXECUTE_IMMEDIATE":
            dynamic_sql_list.append(
                {
                    "line": c_start_line,
                    "end_line": c_end_line,
                    "in_loop": in_lp,
                    "snippet": c_snippet[:140],
                }
            )

        raw_statements.append(
            {
                "verb": verb,
                "start_line": c_start_line,
                "end_line": c_end_line,
                "target_table": target_tbl,
                "source_tables": src_tbls,
                "ctes": ctes,
                "in_loop": in_lp,
                "is_temp_target": is_temp,
                "has_where": has_where,
                "has_select_star": _has_select_star(clause),
                "has_distinct_heavy": _has_distinct_heavy(clause),
                "estimated_tokens": _est_tokens(stmt_raw),
                "section_title": _section_at_line(c_start_line),
                "preview": c_snippet[:100],
            }
        )

    # 未闭合循环也纳入统计
    for unclosed in loop_stack:
        unclosed["end_line"] = total_lines
        loops.append(unclosed)

    # 合并连续大量重复的同表字面量 INSERT VALUES，防止几千条批插膨胀 JSON
    coalesced: List[Dict[str, Any]] = []
    i = 0
    raw_n = len(raw_statements)
    while i < raw_n:
        st = raw_statements[i]
        if (
            st["verb"] == "INSERT"
            and st["target_table"]
            and not st["source_tables"]
            and not st["in_loop"]
        ):
            j = i + 1
            while (
                j < raw_n
                and raw_statements[j]["verb"] == "INSERT"
                and raw_statements[j]["target_table"].lower() == st["target_table"].lower()
                and not raw_statements[j]["source_tables"]
                and not raw_statements[j]["in_loop"]
                and raw_statements[j]["section_title"] == st["section_title"]
            ):
                j += 1
            run_len = j - i
            if run_len >= 5:
                merged = dict(st)
                merged["end_line"] = raw_statements[j - 1]["end_line"]
                merged["estimated_tokens"] = sum(raw_statements[k]["estimated_tokens"] for k in range(i, j))
                merged["repeat_count"] = run_len
                merged["preview"] = f"INSERT INTO {st['target_table']} VALUES (...) [连续 {run_len} 条]"
                coalesced.append(merged)
                i = j
                continue
        coalesced.append(st)
        i += 1

    # 分配 stmt_id (S001, S002, ...) 并运行语句级静态体检
    statements_out: List[Dict[str, Any]] = []
    for idx, st in enumerate(coalesced, start=1):
        sid = f"S{idx:03d}"
        st_rec: Dict[str, Any] = {
            "stmt_id": sid,
            "verb": st["verb"],
            "start_line": st["start_line"],
            "end_line": st["end_line"],
            "target_table": st["target_table"],
            "source_tables": st["source_tables"][:24],
            "ctes": st["ctes"][:12],
            "in_loop": st["in_loop"],
            "estimated_tokens": st["estimated_tokens"],
            "section_title": st["section_title"],
            "preview": st["preview"],
        }
        if st.get("repeat_count"):
            st_rec["repeat_count"] = st["repeat_count"]
        statements_out.append(st_rec)

        verb = st["verb"]
        s_ln = st["start_line"]
        e_ln = st["end_line"]
        tgt = st["target_table"]

        # 1. LOOP_DML [HIGH]
        if st["in_loop"] and (
            verb in ("INSERT", "UPDATE", "DELETE", "MERGE")
            or (verb == "SELECT" and bool(tgt))
        ):
            _add_finding(
                "LOOP_DML",
                "HIGH",
                s_ln,
                e_ln,
                f"循环内逐行执行 {verb}" + (f" ({tgt})" if tgt else ""),
                f"语句 {sid} (L{s_ln}-L{e_ln}) 在游标/循环体内执行 {verb}，海量数据下会产生严重 RBAR 性能瓶颈，建议改写为集合化批处理 SQL。",
                stmt_id=sid,
                table=tgt,
            )

        # 2. UNBOUNDED_DML [HIGH]
        if verb in ("UPDATE", "DELETE") and not st["has_where"]:
            _add_finding(
                "UNBOUNDED_DML",
                "HIGH",
                s_ln,
                e_ln,
                f"缺失 WHERE 条件的全表 {verb}" + (f" ({tgt})" if tgt else ""),
                f"语句 {sid} (L{s_ln}-L{e_ln}) 对 `{tgt or '目标表'}` 执行 {verb} 但未检测到顶层 WHERE 过滤条件，存在全表误删/全表覆盖高危风险！",
                stmt_id=sid,
                table=tgt,
            )

        # 3. DYNAMIC_SQL [MEDIUM]
        if verb == "EXECUTE_IMMEDIATE":
            _add_finding(
                "DYNAMIC_SQL",
                "MEDIUM",
                s_ln,
                e_ln,
                f"动态 SQL 执行 ({sid})",
                f"语句 {sid} (L{s_ln}-L{e_ln}) 使用动态拼接 SQL 执行，存在隐蔽表依赖、SQL 注入及执行计划无法静态校验风险。",
                stmt_id=sid,
            )

        # 4. SELECT_STAR [LOW]
        if verb in ("INSERT", "CTAS", "CREATE_TEMP_TABLE") and st.get("has_select_star"):
            _add_finding(
                "SELECT_STAR",
                "LOW",
                s_ln,
                e_ln,
                f"宽表 {verb} 使用 SELECT *" + (f" -> {tgt}" if tgt else ""),
                f"语句 {sid} (L{s_ln}-L{e_ln}) 使用 `SELECT *` 写入目标表，上游增减列或调整列序时极易引发列错位或报错。",
                stmt_id=sid,
                table=tgt,
            )

        # 5. DISTINCT_HEAVY [LOW]
        if st.get("has_distinct_heavy"):
            _add_finding(
                "DISTINCT_HEAVY",
                "LOW",
                s_ln,
                e_ln,
                f"关联/排序查询包含 DISTINCT ({sid})",
                f"语句 {sid} (L{s_ln}-L{e_ln}) 在多表 JOIN/ORDER BY 上使用 DISTINCT，可能导致重分布与大内存排序，建议核查是否可改用 EXISTS 或消除笛卡尔膨胀。",
                stmt_id=sid,
                table=tgt,
            )

    # 6. DIALECT_COMPAT [MEDIUM]（在非 Oracle 原生方言下扫描遗留语法，剥离注释与字符串后精确定位行号）
    if eff_dialect not in _ORACLE_NATIVE_DIALECTS:
        lines_raw = text.splitlines()
        compat_hits_by_rule: Dict[str, List[int]] = {}
        in_block_cmt = False
        for ln_no, raw_ln in enumerate(lines_raw, start=1):
            work = raw_ln
            if in_block_cmt:
                end_c = work.find("*/")
                if end_c == -1:
                    continue
                work = work[end_c + 2 :]
                in_block_cmt = False
            while "/*" in work:
                s_c = work.find("/*")
                e_c = work.find("*/", s_c + 2)
                if e_c == -1:
                    work = work[:s_c]
                    in_block_cmt = True
                    break
                work = work[:s_c] + " " + work[e_c + 2 :]
            # 去掉行注释与单引号字面量
            dash_pos = work.find("--")
            if dash_pos != -1:
                work = work[:dash_pos]
            work = re.sub(r"'(?:''|[^'])*'", "''", work)
            if not work.strip():
                continue
            for rule_name, pat, _msg in _ORACLE_LEGACY_CHECKS:
                if pat.search(work):
                    compat_hits_by_rule.setdefault(rule_name, []).append(ln_no)

        msg_by_rule = {rn: msg for rn, _p, msg in _ORACLE_LEGACY_CHECKS}
        for rule_name, hit_lines in compat_hits_by_rule.items():
            sample_lns = ", ".join(f"L{x}" for x in hit_lines[:5])
            more_txt = f" 等共 {len(hit_lines)} 处" if len(hit_lines) > 5 else ""
            # 关联首个命中行所属 stmt_id
            hit_sid = ""
            first_ln = hit_lines[0]
            for st_rec in statements_out:
                if st_rec["start_line"] <= first_ln <= st_rec["end_line"]:
                    hit_sid = st_rec["stmt_id"]
                    break
            _add_finding(
                "DIALECT_COMPAT",
                "MEDIUM",
                first_ln,
                hit_lines[-1],
                f"方言兼容风险 [{rule_name}] ({sample_lns}{more_txt})",
                f"{msg_by_rule[rule_name]}（命中行号：{sample_lns}{more_txt}）。",
                stmt_id=hit_sid,
            )

    # 7. 构建数据血缘 DAG
    writers_by_tbl: Dict[str, List[str]] = {}
    readers_by_tbl: Dict[str, List[str]] = {}
    canonical_name: Dict[str, str] = {}
    temp_marked: Set[str] = set()
    edges: List[Dict[str, Any]] = []
    seen_edges: Set[Tuple[str, str, str]] = set()

    for st_idx, st_rec in enumerate(statements_out):
        sid = st_rec["stmt_id"]
        verb = st_rec["verb"]
        tgt = st_rec["target_table"]
        raw_is_temp = coalesced[st_idx]["is_temp_target"]
        if tgt and verb in _WRITE_VERBS:
            tk = tgt.lower()
            canonical_name.setdefault(tk, tgt)
            writers_by_tbl.setdefault(tk, []).append(sid)
            if verb == "CREATE_TEMP_TABLE" or raw_is_temp or _TEMP_NAME_RE.search(tgt.split(".")[-1]):
                temp_marked.add(tk)
        for src in st_rec["source_tables"]:
            sk = src.lower()
            canonical_name.setdefault(sk, src)
            readers_by_tbl.setdefault(sk, []).append(sid)
            if _TEMP_NAME_RE.search(src.split(".")[-1]):
                temp_marked.add(sk)
            if tgt and verb in _WRITE_VERBS and sk != tgt.lower():
                ek = (sk, tgt.lower(), sid)
                if ek not in seen_edges:
                    seen_edges.add(ek)
                    edges.append(
                        {
                            "source": canonical_name[sk],
                            "target": canonical_name[tgt.lower()],
                            "stmt_id": sid,
                            "verb": verb,
                            "start_line": st_rec["start_line"],
                            "end_line": st_rec["end_line"],
                        }
                    )

    all_tbl_keys = sorted(set(writers_by_tbl.keys()) | set(readers_by_tbl.keys()))
    nodes: List[Dict[str, Any]] = []
    role_by_key: Dict[str, str] = {}
    for tk in all_tbl_keys:
        w_sids = writers_by_tbl.get(tk, [])
        r_sids = readers_by_tbl.get(tk, [])
        if tk in temp_marked or (w_sids and r_sids):
            role = "temp_staging"
        elif w_sids and not r_sids:
            role = "final_target"
        else:
            role = "source_only"
        role_by_key[tk] = role
        nodes.append(
            {
                "table": canonical_name[tk],
                "role": role,
                "written_by": w_sids[:12],
                "read_by": r_sids[:12],
            }
        )

    # 生成紧凑可读的血缘链摘要（优先折叠 source -> temp_staging -> final_target 链条）
    edges_from: Dict[str, List[Dict[str, Any]]] = {}
    for e in edges:
        edges_from.setdefault(e["source"].lower(), []).append(e)

    summary_lines: List[str] = []
    covered_edges: Set[Tuple[str, str, str]] = set()

    for e1 in edges:
        mid_k = e1["target"].lower()
        if role_by_key.get(mid_k) == "temp_staging" and mid_k in edges_from:
            for e2 in edges_from[mid_k]:
                if len(summary_lines) >= 18:
                    break
                summary_lines.append(
                    f"[血缘流] {e1['source']} ─({e1['stmt_id']} L{e1['start_line']}-L{e1['end_line']} {e1['verb']})─► "
                    f"{e1['target']} ─({e2['stmt_id']} L{e2['start_line']}-L{e2['end_line']} {e2['verb']})─► {e2['target']}"
                )
                covered_edges.add((e1["source"].lower(), e1["target"].lower(), e1["stmt_id"]))
                covered_edges.add((e2["source"].lower(), e2["target"].lower(), e2["stmt_id"]))
        if len(summary_lines) >= 18:
            break

    for e in edges:
        if len(summary_lines) >= 22:
            break
        ek = (e["source"].lower(), e["target"].lower(), e["stmt_id"])
        if ek in covered_edges:
            continue
        summary_lines.append(
            f"[血缘流] {e['source']} ─({e['stmt_id']} L{e['start_line']}-L{e['end_line']} {e['verb']})─► {e['target']}"
        )

    # 限制持久化的 statements 上限，优先保留写/高危/血缘核心语句（普通超长存储过程通常 < 150 条）
    if len(statements_out) > 250:
        important_ids = {f["stmt_id"] for f in static_findings if f.get("stmt_id")}
        for e in edges:
            important_ids.add(e["stmt_id"])
        kept = [s for s in statements_out if s["stmt_id"] in important_ids or s["verb"] != "SELECT"]
        if len(kept) > 250:
            kept = kept[:250]
        statements_stored = kept
    else:
        statements_stored = statements_out

    finding_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in static_findings:
        sev = f.get("severity", "LOW")
        if sev in finding_counts:
            finding_counts[sev] += 1

    return {
        "version": "1.0",
        "dialect": eff_dialect,
        "line_count": total_lines,
        "total_statement_count": len(raw_statements),
        "statements": statements_stored,
        "lineage_dag": {
            "nodes": nodes[:80],
            "edges": edges[:120],
            "summary_lines": summary_lines,
        },
        "control_flow": {
            "parameters": parameters[:20],
            "variables": variables[:40],
            "cursors": cursors[:30],
            "loops": loops[:30],
            "dynamic_sql": dynamic_sql_list[:20],
            "transactions": transactions[:30],
            "exceptions": exceptions[:20],
        },
        "static_findings": static_findings[:80],
        "finding_counts": finding_counts,
    }


def compact_sql_for_llm(
    text: str,
    *,
    start_line: int = 1,
    max_case_branches: int = 12,
    max_in_items: int = 25,
) -> Dict[str, Any]:
    """生成严格保留原始真实行号 (`L120| ...`) 的高信噪比压缩视图（绝不修改原文件）."""
    if not text:
        return {
            "text": "",
            "original_tokens": 0,
            "compacted_tokens": 0,
            "saved_tokens": 0,
            "reduction_ratio": 0.0,
            "folded_blocks": [],
        }

    orig_tokens = _est_tokens(text)
    raw_lines = text.splitlines()
    n = len(raw_lines)
    folded_blocks: List[Dict[str, Any]] = []

    # 第 1 步：折叠超长单行/多行常量 IN ('01','02',...) 列表（超过 max_in_items 项）
    in_pattern = re.compile(
        r"(?is)\bIN\s*\(\s*((?:(?:'(?:''|[^'])*'|[+-]?\d+(?:\.\d+)?)\s*,\s*){"
        + str(max(5, int(max_in_items)))
        + r",}(?:'(?:''|[^'])*'|[+-]?\d+(?:\.\d+)?))\s*\)"
    )

    line_offsets = _build_line_offsets(text)
    # 若存在跨行大 IN (...) 列表，先在行级标记折叠范围
    skip_line_indices: Set[int] = set()
    replace_line_map: Dict[int, str] = {}

    for m in in_pattern.finditer(text):
        inner = m.group(1)
        items = re.findall(r"'(?:''|[^'])*'|[+-]?\d+(?:\.\d+)?", inner)
        if len(items) <= max_in_items:
            continue
        s_ln_rel = _offset_to_line(line_offsets, m.start()) - 1
        e_ln_rel = _offset_to_line(line_offsets, max(m.start(), m.end() - 1)) - 1
        abs_s = start_line + s_ln_rel
        abs_e = start_line + e_ln_rel
        head_items = ", ".join(items[:5])
        tail_items = ", ".join(items[-3:])
        compact_in = f"IN ({head_items}, /* ... 共 {len(items)} 项常量已折叠 (L{abs_s}-L{abs_e}) ... */, {tail_items})"
        if s_ln_rel == e_ln_rel:
            orig_ln = replace_line_map.get(s_ln_rel, raw_lines[s_ln_rel])
            local_m = in_pattern.search(orig_ln)
            if local_m:
                replace_line_map[s_ln_rel] = orig_ln[: local_m.start()] + compact_in + orig_ln[local_m.end() :]
        else:
            prefix = raw_lines[s_ln_rel][: max(0, m.start() - line_offsets[s_ln_rel])]
            suffix = raw_lines[e_ln_rel][max(0, m.end() - line_offsets[e_ln_rel]) :]
            replace_line_map[s_ln_rel] = f"{prefix}{compact_in}{suffix}"
            for k in range(s_ln_rel + 1, e_ln_rel + 1):
                skip_line_indices.add(k)
        folded_blocks.append(
            {
                "kind": "IN_LIST",
                "start_line": abs_s,
                "end_line": abs_e,
                "item_count": len(items),
            }
        )

    # 第 2 步：逐行扫描，折叠超长静态 CASE WHEN (> max_case_branches)、长注释块 (>= 10 行)、纯装饰横幅与连续空行
    out_lines: List[str] = []
    i = 0
    prev_was_blank = False

    while i < n:
        if i in skip_line_indices:
            i += 1
            continue
        line_str = replace_line_map.get(i, raw_lines[i])
        stripped = line_str.strip()
        abs_ln = start_line + i

        # 剥离纯装饰横幅注释
        if _DECORATIVE_BANNER_RE.match(stripped):
            i += 1
            continue

        # 折叠重复空行
        if not stripped:
            if not prev_was_blank:
                out_lines.append(f"L{abs_ln}| ")
                prev_was_blank = True
            i += 1
            continue
        prev_was_blank = False

        # 检测连续 >= 10 行的纯注释段（如变更日志）
        if stripped.startswith("--"):
            j = i + 1
            while j < n and j not in skip_line_indices and raw_lines[j].strip().startswith("--"):
                j += 1
            run_len = j - i
            if run_len >= 10:
                for k in range(i, i + 2):
                    if not _DECORATIVE_BANNER_RE.match(raw_lines[k].strip()):
                        out_lines.append(f"L{start_line + k}| {raw_lines[k]}")
                mid_s = start_line + i + 2
                mid_e = start_line + j - 2
                out_lines.append(f"    -- ... [已折叠 {run_len - 3} 行连续注释/变更日志, L{mid_s}-L{mid_e}]")
                if not _DECORATIVE_BANNER_RE.match(raw_lines[j - 1].strip()):
                    out_lines.append(f"L{start_line + j - 1}| {raw_lines[j - 1]}")
                folded_blocks.append(
                    {"kind": "COMMENT_BLOCK", "start_line": mid_s, "end_line": mid_e, "line_count": run_len - 3}
                )
                i = j
                continue

        # 检测多行大型静态 CASE WHEN ... THEN ... END 枚举映射（> max_case_branches 分支）
        if re.search(r"(?i)\bCASE\b", stripped) and not re.search(r"(?i)\bEND\b", stripped):
            when_indices: List[int] = []
            end_idx = -1
            nested_case = 0
            scan_limit = min(n, i + 400)
            for j in range(i + 1, scan_limit):
                if j in skip_line_indices:
                    continue
                sj = raw_lines[j].strip()
                if re.search(r"(?i)\bCASE\b", sj) and not re.search(r"(?i)\bEND\b", sj):
                    nested_case += 1
                    continue
                if re.search(r"(?i)\bEND\b", sj):
                    if nested_case > 0:
                        nested_case -= 1
                        continue
                    end_idx = j
                    break
                if nested_case == 0 and re.match(r"(?i)^WHEN\b.*\bTHEN\b", sj):
                    when_indices.append(j)
            if end_idx != -1 and len(when_indices) > max_case_branches:
                # 保留 CASE 头、首 3 条 WHEN、折叠占位说明、末 3 条 WHEN 以及 ELSE/END
                keep_head_end = when_indices[2]
                keep_tail_start = when_indices[-3]
                out_lines.append(f"L{abs_ln}| {line_str}")
                for k in range(i + 1, keep_head_end + 1):
                    if k not in skip_line_indices:
                        out_lines.append(f"L{start_line + k}| {replace_line_map.get(k, raw_lines[k])}")
                fold_s_ln = start_line + keep_head_end + 1
                fold_e_ln = start_line + keep_tail_start - 1
                out_lines.append(
                    f"    -- ... [共 {len(when_indices)} 条同类 CASE WHEN 枚举映射，中间已折叠, L{fold_s_ln}-L{fold_e_ln}]"
                )
                for k in range(keep_tail_start, end_idx + 1):
                    if k not in skip_line_indices:
                        out_lines.append(f"L{start_line + k}| {replace_line_map.get(k, raw_lines[k])}")
                folded_blocks.append(
                    {
                        "kind": "CASE_WHEN",
                        "start_line": fold_s_ln,
                        "end_line": fold_e_ln,
                        "branch_count": len(when_indices),
                    }
                )
                i = end_idx + 1
                continue

        out_lines.append(f"L{abs_ln}| {line_str}")
        i += 1

    compacted_text = "\n".join(out_lines)
    comp_tokens = _est_tokens(compacted_text)
    saved = max(0, orig_tokens - comp_tokens)
    ratio = round(saved / max(1, orig_tokens), 3) if orig_tokens > 0 else 0.0
    return {
        "text": compacted_text,
        "original_tokens": orig_tokens,
        "compacted_tokens": comp_tokens,
        "saved_tokens": saved,
        "reduction_ratio": ratio,
        "folded_blocks": folded_blocks,
    }


def extract_statement_source(
    analysis: Dict[str, Any],
    raw_text: str,
    stmt_id: str,
    *,
    context_before: int = 3,
    context_after: int = 1,
    compact: bool = False,
) -> Optional[Dict[str, Any]]:
    """按 stmt_id（如 `S004` 或 `4`）精确提取完整单条 SQL 原文（附带真实行号与前置注释）."""
    raw_id = str(stmt_id or "").strip().upper()
    if not raw_id:
        return None
    if raw_id.isdigit():
        raw_id = f"S{int(raw_id):03d}"
    elif raw_id.startswith("S") and raw_id[1:].isdigit():
        raw_id = f"S{int(raw_id[1:]):03d}"

    stmts = (analysis or {}).get("statements") or []
    found = next((s for s in stmts if str(s.get("stmt_id") or "").upper() == raw_id), None)
    if found is None:
        return None

    all_lines = (raw_text or "").splitlines()
    total_lines = len(all_lines)
    if total_lines == 0:
        return None

    s_line = max(1, int(found.get("start_line") or 1))
    e_line = min(total_lines, max(s_line, int(found.get("end_line") or s_line)))

    # 向上扩展最多 context_before 行注释上下文
    ext_start = s_line
    for back in range(1, max(0, int(context_before)) + 1):
        cand_ln = s_line - back
        if cand_ln < 1:
            break
        txt = all_lines[cand_ln - 1].strip()
        if txt.startswith("--") or txt.startswith("/*") or not txt:
            ext_start = cand_ln
        else:
            break
    ext_end = min(total_lines, e_line + max(0, int(context_after)))

    slice_text = "\n".join(all_lines[ext_start - 1 : ext_end])
    if compact:
        comp = compact_sql_for_llm(slice_text, start_line=ext_start)
        numbered = comp["text"]
    else:
        numbered = "\n".join(f"L{ext_start + idx}| {ln}" for idx, ln in enumerate(all_lines[ext_start - 1 : ext_end]))

    return {
        "statement": found,
        "start_line": ext_start,
        "end_line": ext_end,
        "total_lines": total_lines,
        "compact": bool(compact),
        "content": numbered,
        "estimated_tokens": _est_tokens(numbered),
    }


def extract_lineage_subgraph(
    analysis: Dict[str, Any],
    raw_text: str,
    table_name: str,
    *,
    depth: int = 3,
    include_downstream: bool = True,
    compact: bool = False,
    max_tokens: int = 16000,
) -> Dict[str, Any]:
    """沿 `lineage_dag` 反向（及可选下游）提取指定表关联的全部核心语句块（手术刀切片）."""
    query_tbl = (table_name or "").strip().strip('"`[]').lower()
    if not query_tbl:
        return {"matched": False, "error": "缺少目标表名 `table_name`"}

    dag = (analysis or {}).get("lineage_dag") or {}
    nodes = dag.get("nodes") or []
    edges = dag.get("edges") or []
    stmts = (analysis or {}).get("statements") or []
    stmt_by_id = {str(s.get("stmt_id")): s for s in stmts if s.get("stmt_id")}

    # 模糊匹配全限定名或表名后缀
    seed_tables: Set[str] = set()
    for nd in nodes:
        t_full = str(nd.get("table") or "").strip().lower()
        t_bare = t_full.split(".")[-1]
        if t_full == query_tbl or t_bare == query_tbl or query_tbl in t_full:
            seed_tables.add(t_full)

    # 若节点中无记录，回退语句中检索
    if not seed_tables:
        for s in stmts:
            tgt = str(s.get("target_table") or "").lower()
            if tgt and (tgt == query_tbl or tgt.split(".")[-1] == query_tbl or query_tbl in tgt):
                seed_tables.add(tgt)
            for src in s.get("source_tables") or []:
                sl = str(src).lower()
                if sl == query_tbl or sl.split(".")[-1] == query_tbl or query_tbl in sl:
                    seed_tables.add(sl)

    if not seed_tables:
        return {
            "matched": False,
            "query_table": table_name,
            "available_tables": [n["table"] for n in nodes[:30]],
            "error": f"在静态血缘图中未找到与 `{table_name}` 匹配的表节点",
        }

    max_depth = max(1, min(6, int(depth or 3)))
    upstream_map: Dict[str, List[Dict[str, Any]]] = {}
    downstream_map: Dict[str, List[Dict[str, Any]]] = {}
    for e in edges:
        sk = str(e.get("source") or "").lower()
        tk = str(e.get("target") or "").lower()
        upstream_map.setdefault(tk, []).append(e)
        downstream_map.setdefault(sk, []).append(e)

    related_tables: Set[str] = set(seed_tables)
    related_stmt_ids: Set[str] = set()
    related_edges: List[Dict[str, Any]] = []
    seen_edge_keys: Set[Tuple[str, str, str]] = set()

    # 反向 BFS 溯源上游
    q_up: deque[Tuple[str, int]] = deque((t, 0) for t in seed_tables)
    visited_up: Set[str] = set(seed_tables)
    while q_up:
        cur_t, dist = q_up.popleft()
        if dist >= max_depth:
            continue
        for e in upstream_map.get(cur_t, []):
            sk = str(e.get("source") or "").lower()
            sid = str(e.get("stmt_id") or "")
            ek = (sk, cur_t, sid)
            if ek not in seen_edge_keys:
                seen_edge_keys.add(ek)
                related_edges.append(e)
            if sid:
                related_stmt_ids.add(sid)
            if sk and sk not in visited_up:
                visited_up.add(sk)
                related_tables.add(sk)
                q_up.append((sk, dist + 1))

    # 可选下游 BFS
    if include_downstream:
        q_dn: deque[Tuple[str, int]] = deque((t, 0) for t in seed_tables)
        visited_dn: Set[str] = set(seed_tables)
        while q_dn:
            cur_t, dist = q_dn.popleft()
            if dist >= max_depth:
                continue
            for e in downstream_map.get(cur_t, []):
                tk = str(e.get("target") or "").lower()
                sid = str(e.get("stmt_id") or "")
                ek = (cur_t, tk, sid)
                if ek not in seen_edge_keys:
                    seen_edge_keys.add(ek)
                    related_edges.append(e)
                if sid:
                    related_stmt_ids.add(sid)
                if tk and tk not in visited_dn:
                    visited_dn.add(tk)
                    related_tables.add(tk)
                    q_dn.append((tk, dist + 1))

    # 补充直接读写 seed_tables 或 related_tables 的其他语句（如 TRUNCATE / DELETE / UPDATE）
    for s in stmts:
        sid = str(s.get("stmt_id") or "")
        tgt = str(s.get("target_table") or "").lower()
        srcs = [str(x).lower() for x in (s.get("source_tables") or [])]
        if tgt in related_tables or any(x in seed_tables for x in srcs):
            related_stmt_ids.add(sid)
        # 始终保留过程头签名以便理解参数
        if s.get("verb") == "CREATE_PROC":
            related_stmt_ids.add(sid)

    ordered_stmts = sorted(
        [stmt_by_id[sid] for sid in related_stmt_ids if sid in stmt_by_id],
        key=lambda x: (int(x.get("start_line") or 0), str(x.get("stmt_id") or "")),
    )

    blocks: List[Dict[str, Any]] = []
    running_tokens = 0
    truncated = False
    budget = max(1500, int(max_tokens or 16000))

    for st in ordered_stmts:
        extracted = extract_statement_source(
            analysis, raw_text, st["stmt_id"], context_before=2, context_after=0, compact=compact
        )
        if not extracted:
            continue
        blk_tok = int(extracted["estimated_tokens"])
        if blocks and running_tokens + blk_tok > budget:
            truncated = True
            break
        blocks.append(
            {
                "stmt_id": st["stmt_id"],
                "verb": st["verb"],
                "start_line": extracted["start_line"],
                "end_line": extracted["end_line"],
                "target_table": st.get("target_table", ""),
                "source_tables": st.get("source_tables", []),
                "section_title": st.get("section_title", ""),
                "content": extracted["content"],
                "estimated_tokens": blk_tok,
            }
        )
        running_tokens += blk_tok

    path_lines = [
        f"{e['source']} ─({e['stmt_id']} L{e['start_line']}-L{e['end_line']} {e['verb']})─► {e['target']}"
        for e in sorted(related_edges, key=lambda x: int(x.get("start_line") or 0))
    ]
    return {
        "matched": True,
        "query_table": table_name,
        "seed_tables": sorted(seed_tables),
        "related_tables": sorted(related_tables),
        "lineage_paths": path_lines[:40],
        "stmt_ids": [b["stmt_id"] for b in blocks],
        "blocks": blocks,
        "estimated_tokens": running_tokens,
        "truncated": truncated,
    }


def format_static_analysis_brief(
    analysis: Dict[str, Any],
    *,
    max_lineage_lines: int = 10,
    max_findings: int = 12,
    max_statements: int = 20,
) -> str:
    """将静态解剖 JSON 渲染为极省 Token 的架构图纸摘要，直接供弱模型开局参考."""
    if not isinstance(analysis, dict) or not analysis.get("statements"):
        return ""

    lines: List[str] = []
    stmts = analysis.get("statements") or []
    dag = analysis.get("lineage_dag") or {}
    cf = analysis.get("control_flow") or {}
    findings = analysis.get("static_findings") or []
    fc = analysis.get("finding_counts") or {}

    nodes = dag.get("nodes") or []
    src_nodes = [n["table"] for n in nodes if n.get("role") == "source_only"]
    tmp_nodes = [n["table"] for n in nodes if n.get("role") == "temp_staging"]
    tgt_nodes = [n["table"] for n in nodes if n.get("role") == "final_target"]

    lines.append(
        f"  SQL 静态解剖图纸：共 {analysis.get('total_statement_count', len(stmts))} 条语句 "
        f"(索引 {len(stmts)} 项) | 静态隐患 HIGH={fc.get('HIGH', 0)} / MEDIUM={fc.get('MEDIUM', 0)} / LOW={fc.get('LOW', 0)}"
    )
    if src_nodes or tmp_nodes or tgt_nodes:
        role_parts = []
        if src_nodes:
            role_parts.append(f"源表({len(src_nodes)}): {', '.join(src_nodes[:8])}{'…' if len(src_nodes) > 8 else ''}")
        if tmp_nodes:
            role_parts.append(f"临时/中间表({len(tmp_nodes)}): {', '.join(tmp_nodes[:8])}{'…' if len(tmp_nodes) > 8 else ''}")
        if tgt_nodes:
            role_parts.append(f"最终目标表({len(tgt_nodes)}): {', '.join(tgt_nodes[:8])}{'…' if len(tgt_nodes) > 8 else ''}")
        lines.append("  表角色拓扑: " + " | ".join(role_parts))

    sum_lines = dag.get("summary_lines") or []
    if sum_lines:
        lines.append("  核心数据血缘 DAG（可用 `trace_attachment_lineage` 按表精准切片）:")
        for sl in sum_lines[:max_lineage_lines]:
            lines.append(f"    {sl}")
        if len(sum_lines) > max_lineage_lines:
            lines.append(f"    ... 其余 {len(sum_lines) - max_lineage_lines} 条血缘链路可通过 `get_attachment_static_report` 查看")

    cf_parts: List[str] = []
    if cf.get("parameters"):
        cf_parts.append(f"参数 {len(cf['parameters'])} 个")
    if cf.get("variables"):
        cf_parts.append(f"变量 {len(cf['variables'])} 个")
    if cf.get("cursors"):
        cf_parts.append(f"游标 {len(cf['cursors'])} 个")
    if cf.get("loops"):
        lp_desc = ", ".join(f"{l['kind']}(L{l['start_line']}-L{l['end_line']})" for l in cf["loops"][:4])
        cf_parts.append(f"循环 {len(cf['loops'])} 处 [{lp_desc}]")
    if cf.get("dynamic_sql"):
        cf_parts.append(f"动态SQL {len(cf['dynamic_sql'])} 处")
    if cf.get("transactions"):
        tx_desc = ", ".join(f"{t['verb']}@L{t['line']}" for t in cf["transactions"][:4])
        cf_parts.append(f"事务边界 [{tx_desc}]")
    if cf.get("exceptions"):
        cf_parts.append(f"异常捕获 {len(cf['exceptions'])} 处")
    if cf_parts:
        lines.append("  控制流骨架: " + " | ".join(cf_parts))

    if findings:
        lines.append("  静态隐患雷达（请逐项结合源码核实判定，勿遗漏 HIGH/MEDIUM）:")
        # HIGH 优先，再 MEDIUM，再 LOW
        sev_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        sorted_f = sorted(findings, key=lambda x: (sev_rank.get(x.get("severity", "LOW"), 3), int(x.get("line") or 0)))
        for f in sorted_f[:max_findings]:
            sid_tag = f" {f['stmt_id']}" if f.get("stmt_id") else ""
            lines.append(
                f"    - [{f['finding_id']}][{f['severity']}][{f['code']}]{sid_tag} L{f['line']}: {f['title']}"
            )
        if len(findings) > max_findings:
            lines.append(f"    - ... 其余 {len(findings) - max_findings} 项可通过 `get_attachment_static_report` 查看")

    if stmts:
        lines.append("  关键语句索引（可用 `read_attachment_statement` 按 stmt_id 完整精读）:")
        for s in stmts[:max_statements]:
            tgt_tag = f" -> {s['target_table']}" if s.get("target_table") else ""
            lp_tag = " [IN_LOOP]" if s.get("in_loop") else ""
            lines.append(
                f"    - {s['stmt_id']} [{s['verb']}] L{s['start_line']}-L{s['end_line']}{tgt_tag}{lp_tag} (~{s.get('estimated_tokens', 0)} tok)"
            )
        if len(stmts) > max_statements:
            lines.append(f"    - ... 共 {len(stmts)} 条语句索引，可按 `stmt_id` 或 `trace_attachment_lineage` 精准查阅")

    return "\n".join(lines)
