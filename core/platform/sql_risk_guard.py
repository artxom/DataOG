"""Data.O.G SQL 写操作风险分级器与词法级安全多语句切分器 (sql_risk_guard).

核心职责：
1. 复用与 `readonly_guard` 一致的确定性词法扫描约定，在引号、注释、`$$` Dollar-Quoting 外安全切分多条 SQL 语句；
2. 后端独立做词法风险定级（绝不信任前端传入的 risk_level），区分：
   - `READONLY`：只读查询（通过 `validate_readonly_sql`）
   - `STANDARD_WRITE`：常规 DDL/写入（`CREATE` / `ALTER` 非删列 / `COMMENT` / `INSERT` / `MERGE` / `UPSERT` / `REPLACE` / `ANALYZE` / `VACUUM`）
   - `HIGH_RISK`：破坏性或高危语句（`DROP` / `TRUNCATE` / `DELETE` / `UPDATE` / `GRANT` / `REVOKE` / `ALTER ... DROP` 等），强制需要输入 `CONFIRM` 确认词；
3. 为单表 `DELETE` / `UPDATE` 生成经 `validate_readonly_sql` 二次验证的只读影响行数预估 SQL (`SELECT COUNT(*) ...`)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import List, Optional, Set

from core.platform.readonly_guard import ReadOnlyViolationError, validate_readonly_sql

_DOLLAR_TAG_RE = re.compile(r"^\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")

HIGH_RISK_KEYWORDS: Set[str] = {
    "DROP",
    "TRUNCATE",
    "DELETE",
    "UPDATE",
    "GRANT",
    "REVOKE",
    "KILL",
    "SHUTDOWN",
    "PURGE",
    "ATTACH",
    "DETACH",
    "COPY",
    "LOAD",
    "UNLOAD",
    "CALL",
    "EXEC",
    "EXECUTE",
    "DO",
}

STANDARD_WRITE_VERBS: Set[str] = {
    "CREATE",
    "ALTER",
    "COMMENT",
    "INSERT",
    "MERGE",
    "UPSERT",
    "REPLACE",
    "ANALYZE",
    "VACUUM",
}

_DDL_VERBS: Set[str] = {"CREATE", "ALTER", "DROP", "TRUNCATE", "COMMENT", "RENAME"}


@dataclass(frozen=True)
class _LexToken:
    kind: str  # WORD | QIDENT | STRING | NUMBER | LP | RP | SEMI | COMMA | DOT | DOLLAR | OP
    upper: str
    raw: str
    start: int
    end: int


@dataclass
class SqlStatementRisk:
    index: int
    sql: str
    primary_verb: str
    category: str  # READONLY | DDL | DML | DCL | OTHER
    risk_level: str  # READONLY | STANDARD_WRITE | HIGH_RISK
    requires_confirm_token: bool
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "sql": self.sql,
            "primary_verb": self.primary_verb,
            "category": self.category,
            "risk_level": self.risk_level,
            "requires_confirm_token": self.requires_confirm_token,
            "warnings": list(self.warnings),
        }


@dataclass
class BatchRiskAssessment:
    statements: List[str]
    statement_risks: List[SqlStatementRisk]
    risk_level: str  # READONLY | STANDARD_WRITE | HIGH_RISK
    category: str
    requires_confirm_token: bool
    warnings: List[str]
    sql_hash: str
    statement_count: int

    def to_dict(self) -> dict:
        return {
            "statements": list(self.statements),
            "statement_risks": [r.to_dict() for r in self.statement_risks],
            "risk_level": self.risk_level,
            "category": self.category,
            "requires_confirm_token": self.requires_confirm_token,
            "warnings": list(self.warnings),
            "sql_hash": self.sql_hash,
            "statement_count": self.statement_count,
        }


def _scan_sql_tokens(sql: str, *, backslash_escape: bool = True) -> List[_LexToken]:
    """容错确定性词法扫描：支持标准引号、反斜杠转义、块/行注释、$tag$ 块，记录字符起止偏移."""
    tokens: List[_LexToken] = []
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

        # 块注释 /* ... */
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end_idx = sql.find("*/", i + 2)
            if end_idx == -1:
                raise ValueError("SQL 存在未闭合的块注释 (/*)")
            i = end_idx + 2
            continue

        # Dollar-Quoting $tag$...$tag$
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql[i:])
            if m:
                tag = m.group(0)
                close_pos = sql.find(tag, i + len(tag))
                if close_pos == -1:
                    raise ValueError(f"SQL 存在未闭合的 Dollar-Quoting 块 ({tag})")
                end_pos = close_pos + len(tag)
                tokens.append(_LexToken("DOLLAR", "'__DOLLAR__'", sql[i:end_pos], i, end_pos))
                i = end_pos
                continue
            tokens.append(_LexToken("OP", "$", "$", i, i + 1))
            i += 1
            continue

        # 单引号字符串
        if ch == "'" or (
            ch in ("E", "e", "N", "n", "X", "x", "B", "b")
            and i + 1 < n
            and sql[i + 1] == "'"
            and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_"))
        ):
            q_start = i + 1 if ch != "'" else i
            j = q_start + 1
            closed = False
            while j < n:
                c = sql[j]
                if backslash_escape and c == "\\":
                    if j + 1 >= n:
                        break
                    j += 2
                    continue
                if c == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    closed = True
                    j += 1
                    break
                j += 1
            if not closed:
                raise ValueError("SQL 存在未闭合的单引号字符串")
            tokens.append(_LexToken("STRING", "'__LITERAL__'", sql[i:j], i, j))
            i = j
            continue

        # 引用标识符 ("..." / `...` / [...])
        if ch in ('"', "`", "["):
            close_ch = "]" if ch == "[" else ch
            j = i + 1
            inner_chars: List[str] = []
            closed = False
            while j < n:
                c = sql[j]
                if backslash_escape and ch in ('"', "`") and c == "\\":
                    if j + 1 >= n:
                        break
                    inner_chars.append(sql[j + 1])
                    j += 2
                    continue
                if c == close_ch:
                    if j + 1 < n and sql[j + 1] == close_ch:
                        inner_chars.append(close_ch)
                        j += 2
                        continue
                    closed = True
                    j += 1
                    break
                inner_chars.append(c)
                j += 1
            if not closed:
                raise ValueError(f"SQL 存在未闭合的引用标识符 ({ch})")
            inner = "".join(inner_chars).strip()
            tokens.append(_LexToken("QIDENT", inner.upper(), sql[i:j], i, j))
            i = j
            continue

        if ch == "(":
            tokens.append(_LexToken("LP", "(", "(", i, i + 1))
            i += 1
            continue
        if ch == ")":
            tokens.append(_LexToken("RP", ")", ")", i, i + 1))
            i += 1
            continue
        if ch == ";":
            tokens.append(_LexToken("SEMI", ";", ";", i, i + 1))
            i += 1
            continue
        if ch == ",":
            tokens.append(_LexToken("COMMA", ",", ",", i, i + 1))
            i += 1
            continue
        if ch == ".":
            tokens.append(_LexToken("DOT", ".", ".", i, i + 1))
            i += 1
            continue

        if ch.isdigit():
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in (".", "_")):
                j += 1
            tokens.append(_LexToken("NUMBER", sql[i:j].upper(), sql[i:j], i, j))
            i = j
            continue

        if ch.isalpha() or ch == "_" or ord(ch) >= 128:
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_" or ord(sql[j]) >= 128):
                j += 1
            raw_word = sql[i:j]
            tokens.append(_LexToken("WORD", raw_word.upper(), raw_word, i, j))
            i = j
            continue

        tokens.append(_LexToken("OP", ch, ch, i, i + 1))
        i += 1

    return tokens


def split_sql_statements(sql_text: str, *, max_statements: int = 20) -> List[str]:
    """在引号、注释及 Dollar-Quoting 块之外，按顶层分号 `;` 安全切分多条 SQL 语句."""
    if not isinstance(sql_text, str):
        raise ValueError("SQL 语句必须为字符串")
    if "\x00" in sql_text:
        raise ValueError("SQL 语句包含非法空字节 (NUL)")
    raw = sql_text.strip()
    if not raw:
        return []

    tokens = _scan_sql_tokens(raw, backslash_escape=True)
    # 同时做 ANSI 引号无转义模式括号/引号校验，防反斜杠歧义切分
    _scan_sql_tokens(raw, backslash_escape=False)

    segments: List[str] = []
    depth = 0
    seg_start = 0
    for tok in tokens:
        if tok.kind == "LP":
            depth += 1
        elif tok.kind == "RP":
            depth = max(0, depth - 1)
        elif tok.kind == "SEMI" and depth == 0:
            part = raw[seg_start : tok.start].strip()
            if part:
                # 仅包含注释而无语义 token 的片段跳过
                if any(t.start >= seg_start and t.end <= tok.start for t in tokens if t.kind != "SEMI"):
                    segments.append(part)
            seg_start = tok.end

    tail = raw[seg_start:].strip()
    if tail:
        if any(t.start >= seg_start for t in tokens if t.kind != "SEMI"):
            segments.append(tail)

    if len(segments) > max_statements:
        raise ValueError(f"单次提交 SQL 语句数量 ({len(segments)}) 超出上限 {max_statements} 条")
    return segments


def _infer_category(verb: str) -> str:
    v = (verb or "").upper()
    if v in ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"):
        return "READONLY"
    if v in _DDL_VERBS:
        return "DDL"
    if v in ("INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "REPLACE"):
        return "DML"
    if v in ("GRANT", "REVOKE"):
        return "DCL"
    return "OTHER"


def classify_sql_statement(
    stmt: str,
    db_type: str = "",
    *,
    index: int = 1,
) -> SqlStatementRisk:
    """对单条 SQL 执行词法级风险定级."""
    clean = (stmt or "").strip()
    if not clean:
        raise ValueError("SQL 语句不能为空")

    try:
        validate_readonly_sql(clean, db_type=db_type)
        tokens_ro = _scan_sql_tokens(clean, backslash_escape=False)
        verb = tokens_ro[0].upper if tokens_ro else "SELECT"
        return SqlStatementRisk(
            index=index,
            sql=clean,
            primary_verb=verb,
            category="READONLY",
            risk_level="READONLY",
            requires_confirm_token=False,
            warnings=[],
        )
    except ReadOnlyViolationError:
        pass

    tokens_ansi = _scan_sql_tokens(clean, backslash_escape=False)
    tokens_bs = _scan_sql_tokens(clean, backslash_escape=True)
    if not tokens_ansi:
        raise ValueError("SQL 语句仅包含注释或空白")

    first = tokens_ansi[0]
    verb = first.upper if first.kind == "WORD" else first.raw.upper()
    category = _infer_category(verb)

    warnings: List[str] = []
    all_word_sets = [
        {t.upper for t in tokens_ansi if t.kind == "WORD"},
        {t.upper for t in tokens_bs if t.kind == "WORD"},
    ]
    merged_words = all_word_sets[0] | all_word_sets[1]

    # 判断是否高危
    is_high_risk = False
    if verb in HIGH_RISK_KEYWORDS:
        is_high_risk = True
    elif verb not in STANDARD_WRITE_VERBS:
        is_high_risk = True
        warnings.append(f"非常规或未知语句类型 [{verb}]，默认按高危变更处理。")
    else:
        # 即使首词是 CREATE/ALTER/INSERT/MERGE，若内部夹带高危词（如 ALTER TABLE DROP COLUMN / WITH DELETE / CREATE ... DROP），按高危拦截
        for kw in HIGH_RISK_KEYWORDS:
            if kw in merged_words:
                if verb == "ALTER" and kw == "DROP":
                    warnings.append("检测到 ALTER ... DROP 结构删除子句，存在元数据或列数据丢失风险。")
                    is_high_risk = True
                    break
                if kw in merged_words and kw != verb:
                    warnings.append(f"语句中包含高危关键字 [{kw}]。")
                    is_high_risk = True
                    break

    # 检测无 WHERE 条件的 DELETE / UPDATE
    if verb in ("DELETE", "UPDATE"):
        depth = 0
        has_top_where = False
        for t in tokens_ansi:
            if t.kind == "LP":
                depth += 1
            elif t.kind == "RP":
                depth = max(0, depth - 1)
            elif depth == 0 and t.kind == "WORD" and t.upper == "WHERE":
                has_top_where = True
                break
        if not has_top_where:
            warnings.append("警告：该语句不包含 WHERE 条件，可能影响整张表数据！")

    if verb in ("DROP", "TRUNCATE"):
        warnings.append(f"执行 {verb} 操作将直接移除对象或清空全表数据，请务必核实目标连接与对象名称。")

    risk_level = "HIGH_RISK" if is_high_risk else "STANDARD_WRITE"
    return SqlStatementRisk(
        index=index,
        sql=clean,
        primary_verb=verb,
        category=category,
        risk_level=risk_level,
        requires_confirm_token=is_high_risk,
        warnings=warnings,
    )


def classify_sql_batch(
    sql_text: str,
    db_type: str = "",
    *,
    max_statements: int = 20,
) -> BatchRiskAssessment:
    """切分并对整批 SQL 语句执行综合风险评估."""
    stmts = split_sql_statements(sql_text, max_statements=max_statements)
    if not stmts:
        raise ValueError("未检测到可执行的有效 SQL 语句")

    risks: List[SqlStatementRisk] = []
    for idx, stmt in enumerate(stmts, start=1):
        risks.append(classify_sql_statement(stmt, db_type=db_type, index=idx))

    rank = {"READONLY": 0, "STANDARD_WRITE": 1, "HIGH_RISK": 2}
    highest = max(risks, key=lambda r: rank.get(r.risk_level, 2))
    risk_level = highest.risk_level

    categories = {r.category for r in risks if r.category != "READONLY"}
    if len(categories) == 1:
        category = next(iter(categories))
    elif "DDL" in categories and "DML" in categories:
        category = "DDL+DML"
    elif categories:
        category = highest.category
    else:
        category = "READONLY"

    dedup_warnings: List[str] = []
    for r in risks:
        for w in r.warnings:
            msg = f"第 {r.index} 条 ({r.primary_verb}): {w}" if len(risks) > 1 else w
            if msg not in dedup_warnings:
                dedup_warnings.append(msg)

    normalized = "\n;\n".join(stmts)
    sql_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return BatchRiskAssessment(
        statements=stmts,
        statement_risks=risks,
        risk_level=risk_level,
        category=category,
        requires_confirm_token=(risk_level == "HIGH_RISK"),
        warnings=dedup_warnings,
        sql_hash=sql_hash,
        statement_count=len(stmts),
    )


def build_affected_rows_probe_sql(stmt: str, db_type: str = "") -> Optional[str]:
    """为单表 DELETE/UPDATE 构造安全的只读影响行数预估 SQL (`SELECT COUNT(*)`)；复杂语句返回 None."""
    clean = (stmt or "").strip()
    if not clean:
        return None
    try:
        tokens = _scan_sql_tokens(clean, backslash_escape=False)
    except ValueError:
        return None
    if len(tokens) < 3:
        return None
    if tokens[-1].kind == "SEMI":
        tokens = tokens[:-1]
    if not tokens or tokens[0].kind != "WORD":
        return None

    verb = tokens[0].upper
    idx = 1
    if verb == "DELETE":
        if idx < len(tokens) and tokens[idx].kind == "WORD" and tokens[idx].upper == "FROM":
            idx += 1
        elif idx >= len(tokens):
            return None
        # 跳过 PG/Oracle 可选 ONLY
        if idx < len(tokens) and tokens[idx].kind == "WORD" and tokens[idx].upper == "ONLY":
            idx += 1
    elif verb == "UPDATE":
        if idx < len(tokens) and tokens[idx].kind == "WORD" and tokens[idx].upper == "ONLY":
            idx += 1
    else:
        return None

    if idx >= len(tokens):
        return None

    # 解析目标表名 [schema.]table
    tbl_start = tokens[idx].start
    if tokens[idx].kind not in ("WORD", "QIDENT"):
        return None
    tbl_end = tokens[idx].end
    idx += 1
    while idx + 1 < len(tokens) and tokens[idx].kind == "DOT" and tokens[idx + 1].kind in ("WORD", "QIDENT"):
        tbl_end = tokens[idx + 1].end
        idx += 2
    table_ref = clean[tbl_start:tbl_end]

    # 定位顶层 WHERE 位置（跳过 SET 赋值或别名，禁止多表 JOIN 误判）
    depth = 0
    where_tok_idx: Optional[int] = None
    for j in range(idx, len(tokens)):
        t = tokens[j]
        if t.kind == "LP":
            depth += 1
        elif t.kind == "RP":
            depth = max(0, depth - 1)
        elif depth == 0 and t.kind == "WORD":
            if t.upper in ("JOIN", "USING", "RETURNING", "OUTPUT"):
                return None
            if t.upper == "WHERE":
                where_tok_idx = j
                break

    if where_tok_idx is None:
        probe = f"SELECT COUNT(*) AS estimated_rows FROM {table_ref}"
    else:
        # 截断到 ORDER BY / LIMIT / RETURNING（若顶层存在）之前
        cond_start = tokens[where_tok_idx].end
        cond_end = len(clean)
        depth = 0
        for j in range(where_tok_idx + 1, len(tokens)):
            t = tokens[j]
            if t.kind == "LP":
                depth += 1
            elif t.kind == "RP":
                depth = max(0, depth - 1)
            elif depth == 0 and t.kind == "WORD" and t.upper in ("RETURNING", "OUTPUT", "ORDER", "LIMIT"):
                cond_end = t.start
                break
        where_body = clean[cond_start:cond_end].strip()
        if not where_body:
            return None
        probe = f"SELECT COUNT(*) AS estimated_rows FROM {table_ref} WHERE {where_body}"

    try:
        return validate_readonly_sql(probe, db_type=db_type)
    except ReadOnlyViolationError:
        return None
