"""Data.O.G 词法与语法级只读 SQL 白名单安全沙箱 (ReadOnlySqlGuard).

安全定位与边界说明：
- 本模块隶属平台底座 (`core/platform/`)，为 AI 智能数据探查与只读查询通道提供第一道硬隔离防线（配合数据库驱动会话级只读锁与 finally 无条件 rollback 构成纵深防御）。
- 绝不依赖大模型 Prompt 软约束：通过「ANSI 标准引号 + 反斜杠转义」双模态词法扫描，封堵跨方言引号逃逸 (`\\'`), 可执行条件注释 (`/*!`), 嵌套块注释, Dollar-Quoting (`$$`), 多语句堆叠 (`;`), 数据修改 CTE (`WITH ... DELETE`), `SELECT INTO` / `INTO OUTFILE`, 行表锁 (`FOR UPDATE/SHARE`) 及跨数据库危险系统函数。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List, Set, Tuple


class ReadOnlyViolationError(ValueError):
    """当 SQL 违反只读安全沙箱策略时抛出的硬阻断异常."""


@dataclass(frozen=True)
class SqlToken:
    kind: str  # WORD | QIDENT | STRING | NUMBER | LP | RP | SEMI | COMMA | DOT | OP
    upper: str
    raw: str
    pos: int = 0


_DOLLAR_TAG_RE = re.compile(r"^\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")

# 顶层允许的只读起始动词
_ALLOWED_TOP_VERBS: Set[str] = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}

# 全语句无条件禁止的未加引号关键字（出现即阻断）
_BANNED_UNQUOTED_WORDS: Set[str] = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "UPSERT",
    "DROP",
    "ALTER",
    "RENAME",
    "GRANT",
    "REVOKE",
    "VACUUM",
    "ANALYZE",
    "CALL",
    "EXEC",
    "EXECUTE",
    "PREPARE",
    "DEALLOCATE",
    "COPY",
    "UNLOAD",
    "LOAD",
    "ATTACH",
    "DETACH",
    "REINDEX",
    "PRAGMA",
    "BEGIN",
    "START",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "DECLARE",
    "HANDLER",
    "DO",
    "USE",
    "KILL",
    "SHUTDOWN",
    "FLUSH",
    "RESET",
    "PURGE",
    "OPTIMIZE",
    "REPAIR",
    "CHECKPOINT",
    "CLUSTER",
    "LISTEN",
    "NOTIFY",
    "UNLISTEN",
    "IMPORT",
    "EXPORT",
    "LOCK",
    "INTO",
}

# SHOW 允许紧跟的安全元数据查看目标
_ALLOWED_SHOW_PREFIXES: Set[Tuple[str, ...]] = {
    ("TABLES",),
    ("FULL", "TABLES"),
    ("VIEWS",),
    ("DATABASES",),
    ("SCHEMAS",),
    ("COLUMNS",),
    ("FULL", "COLUMNS"),
    ("FIELDS",),
    ("FULL", "FIELDS"),
    ("INDEX",),
    ("INDEXES",),
    ("KEYS",),
    ("PARTITIONS",),
    ("CREATE", "TABLE"),
    ("CREATE", "VIEW"),
    ("TABLE", "STATUS"),
}

# 跨方言危险系统控制、文件读写、外部网络、锁等待与序列副作用函数黑名单
_DANGEROUS_FUNCTIONS: Set[str] = {
    # PostgreSQL / DWS / openGauss / Kingbase
    "PG_TERMINATE_BACKEND",
    "PG_CANCEL_BACKEND",
    "PG_RELOAD_CONF",
    "PG_ROTATE_LOGFILE",
    "PG_READ_FILE",
    "PG_READ_BINARY_FILE",
    "PG_LS_DIR",
    "PG_LS_LOGDIR",
    "PG_LS_WALDIR",
    "PG_LS_TMPDIR",
    "PG_LS_ARCHIVE_STATUSDIR",
    "PG_STAT_FILE",
    "PG_SLEEP",
    "PG_SLEEP_FOR",
    "PG_SLEEP_UNTIL",
    "PG_ADVISORY_LOCK",
    "PG_ADVISORY_LOCK_SHARED",
    "PG_TRY_ADVISORY_LOCK",
    "PG_TRY_ADVISORY_LOCK_SHARED",
    "PG_ADVISORY_XACT_LOCK",
    "PG_ADVISORY_XACT_LOCK_SHARED",
    "PG_TRY_ADVISORY_XACT_LOCK",
    "PG_ADVISORY_UNLOCK",
    "PG_ADVISORY_UNLOCK_ALL",
    "PG_CREATE_RESTORE_POINT",
    "PG_SWITCH_WAL",
    "PG_SWITCH_XLOG",
    "PG_BACKUP_START",
    "PG_BACKUP_STOP",
    "PG_START_BACKUP",
    "PG_STOP_BACKUP",
    "PG_PROMOTE",
    "PG_IMPORT_SYSTEM_COLLATIONS",
    "PG_CREATE_PHYSICAL_REPLICATION_SLOT",
    "PG_CREATE_LOGICAL_REPLICATION_SLOT",
    "PG_DROP_REPLICATION_SLOT",
    "PG_COPY_PHYSICAL_REPLICATION_SLOT",
    "PG_COPY_LOGICAL_REPLICATION_SLOT",
    "PG_LOG_BACKEND_MEMORY_CONTEXTS",
    "LO_IMPORT",
    "LO_EXPORT",
    "LO_UNLINK",
    "LO_CREATE",
    "LO_CREAT",
    "LO_OPEN",
    "LO_WRITE",
    "LO_PUT",
    "LO_FROM_BYTEA",
    "LOREAD",
    "LOWRITE",
    "DBLINK",
    "DBLINK_EXEC",
    "DBLINK_CONNECT",
    "DBLINK_CONNECT_U",
    "DBLINK_DISCONNECT",
    "DBLINK_SEND_QUERY",
    "DBLINK_OPEN",
    "SET_CONFIG",
    "NEXTVAL",
    "SETVAL",
    "QUERY_TO_XML",
    "TABLE_TO_XML",
    "CURSOR_TO_XML",
    "DATABASE_TO_XML",
    # MySQL / MariaDB / TiDB / Doris / StarRocks
    "SLEEP",
    "BENCHMARK",
    "LOAD_FILE",
    "GET_LOCK",
    "RELEASE_LOCK",
    "RELEASE_ALL_LOCKS",
    "MASTER_POS_WAIT",
    "SOURCE_POS_WAIT",
    "WAIT_FOR_EXECUTED_GTID_SET",
    "WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS",
    # SQLite
    "LOAD_EXTENSION",
    "FTS3_TOKENIZER",
    "WRITEFILE",
    "READFILE",
    "EDIT",
    # SQL Server / Sybase / 通用存储过程扩展
    "XP_CMDSHELL",
    "SP_OACREATE",
}

# Oracle / DM8 / DB2 危险内置系统包前缀（命中 PKG.PROC 即阻断）
_DANGEROUS_PACKAGES: Set[str] = {
    "UTL_FILE",
    "UTL_HTTP",
    "UTL_TCP",
    "UTL_SMTP",
    "UTL_INADDR",
    "DBMS_LOCK",
    "DBMS_SESSION",
    "DBMS_PIPE",
    "DBMS_SCHEDULER",
    "DBMS_JOB",
    "DBMS_SQL",
    "DBMS_XMLGEN",
    "DBMS_LDAP",
}


def _lex_sql(
    sql: str,
    *,
    backslash_escape: bool,
    allow_pg_hash_op: bool,
) -> List[SqlToken]:
    """单遍确定性词法扫描：校验注释、引号、括号闭合性并产出语义 Token 流."""
    tokens: List[SqlToken] = []
    n = len(sql)
    i = 0
    paren_depth = 0

    while i < n:
        ch = sql[i]

        if ch.isspace():
            i += 1
            continue

        # 1. 标准单行注释 --
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i + 2)
            i = n if nl == -1 else nl + 1
            continue

        # 2. 块注释 /* ... */（严禁 MySQL /*! 条件可执行注释与嵌套 /*）
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            if i + 2 < n and (
                sql[i + 2] == "!"
                or (sql[i + 2] in ("M", "m") and i + 3 < n and sql[i + 3] == "!")
            ):
                raise ReadOnlyViolationError(
                    "只读沙箱严禁使用可执行条件注释 (/*! ... */)"
                )
            j = i + 2
            closed = False
            while j < n:
                if sql[j] == "/" and j + 1 < n and sql[j + 1] == "*":
                    raise ReadOnlyViolationError(
                        "只读沙箱严禁使用嵌套块注释 (/* ... /*)"
                    )
                if sql[j] == "*" and j + 1 < n and sql[j + 1] == "/":
                    i = j + 2
                    closed = True
                    break
                j += 1
            if not closed:
                raise ReadOnlyViolationError("SQL 存在未闭合的块注释 (/*)")
            continue

        # 3. '#' 符号：PG/DWS 仅放行 #> / #>> JSON 路径操作符，其余一律拦截以阻断跨方言注释截断引号攻击
        if ch == "#":
            if allow_pg_hash_op and i + 1 < n and sql[i + 1] == ">":
                if i + 2 < n and sql[i + 2] == ">":
                    tokens.append(SqlToken("OP", "#>>", "#>>"))
                    i += 3
                else:
                    tokens.append(SqlToken("OP", "#>", "#>"))
                    i += 2
                continue
            raise ReadOnlyViolationError(
                "只读沙箱禁止在字符串外使用 '#' 非标准注释或临时表符号"
            )

        # 4. Dollar-Quoting ($$ 或 $tag$) 严厉禁止
        if ch == "$":
            if _DOLLAR_TAG_RE.match(sql[i:]):
                raise ReadOnlyViolationError(
                    "只读沙箱严禁使用 Dollar-Quoting ($$) 匿名块或字符串语法"
                )
            raise ReadOnlyViolationError(
                "只读沙箱禁止在字符串外使用 '$' 参数占位或特殊语法"
            )

        # 5. Unicode 转义字面量 U&'...' 禁止（防止 UESCAPE 自定义转义符绕过）
        if (
            ch in ("U", "u")
            and i + 2 < n
            and sql[i + 1] == "&"
            and sql[i + 2] in ("'", '"')
        ):
            raise ReadOnlyViolationError(
                "只读沙箱禁止使用 U& Unicode 自定义转义语法"
            )

        # 6. 单引号字符串字面量（含 E'...', N'...', X'...', B'...' 前缀）
        if ch == "'" or (
            ch in ("E", "e", "N", "n", "X", "x", "B", "b")
            and i + 1 < n
            and sql[i + 1] == "'"
            and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_"))
        ):
            quote_start = i + 1 if ch != "'" else i
            j = quote_start + 1
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
                raise ReadOnlyViolationError("SQL 存在未闭合或存在转义歧义的单引号字符串")
            tokens.append(SqlToken("STRING", "'__LITERAL__'", sql[i:j]))
            i = j
            continue

        # 7. 双引号 / 反引号 / 方括号引用标识符
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
                raise ReadOnlyViolationError(f"SQL 存在未闭合的引用标识符 ({ch})")
            inner = "".join(inner_chars).strip()
            if not inner:
                raise ReadOnlyViolationError("SQL 存在空的引用标识符")
            tokens.append(SqlToken("QIDENT", inner.upper(), sql[i:j]))
            i = j
            continue

        # 8. 结构分隔符与括号
        if ch == "(":
            paren_depth += 1
            tokens.append(SqlToken("LP", "(", "("))
            i += 1
            continue
        if ch == ")":
            paren_depth -= 1
            if paren_depth < 0:
                raise ReadOnlyViolationError("SQL 存在未配对的右括号 ')'")
            tokens.append(SqlToken("RP", ")", ")"))
            i += 1
            continue
        if ch == ";":
            tokens.append(SqlToken("SEMI", ";", ";", pos=i))
            i += 1
            continue
        if ch == ",":
            tokens.append(SqlToken("COMMA", ",", ","))
            i += 1
            continue
        if ch == ".":
            tokens.append(SqlToken("DOT", ".", "."))
            i += 1
            continue

        # 9. 数值字面量
        if ch.isdigit():
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in (".", "_")):
                j += 1
            raw_num = sql[i:j]
            tokens.append(SqlToken("NUMBER", raw_num.upper(), raw_num))
            i = j
            continue

        # 10. 未加引号关键字或标识符（兼容中文字段/表名）
        if ch.isalpha() or ch == "_" or ord(ch) >= 128:
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_" or ord(sql[j]) >= 128):
                j += 1
            raw_word = sql[i:j]
            tokens.append(SqlToken("WORD", raw_word.upper(), raw_word))
            i = j
            continue

        # 11. 其他常规比较与算术操作符
        tokens.append(SqlToken("OP", ch, ch))
        i += 1

    if paren_depth != 0:
        raise ReadOnlyViolationError("SQL 存在未闭合的左括号 '('")

    return tokens


def _find_matching_rp(tokens: List[SqlToken], lp_index: int) -> int:
    """给定 LP 位置，返回配对的 RP 索引."""
    depth = 0
    for idx in range(lp_index, len(tokens)):
        if tokens[idx].kind == "LP":
            depth += 1
        elif tokens[idx].kind == "RP":
            depth -= 1
            if depth == 0:
                return idx
    raise ReadOnlyViolationError("SQL 括号配对不完整")


def _validate_with_cte_structure(tokens: List[SqlToken]) -> None:
    """校验 WITH 语句的所有 CTE 子查询及最终主查询必须严格为只读 SELECT."""
    n = len(tokens)
    idx = 1  # 跳过开头的 WITH
    if idx < n and tokens[idx].kind == "WORD" and tokens[idx].upper == "RECURSIVE":
        idx += 1

    cte_count = 0
    while idx < n:
        if tokens[idx].kind not in ("WORD", "QIDENT"):
            raise ReadOnlyViolationError("WITH CTE 缺少合法的临时视图名称")
        idx += 1

        # 可选列名列表: cte_name (c1, c2)
        if idx < n and tokens[idx].kind == "LP":
            rp_cols = _find_matching_rp(tokens, idx)
            idx = rp_cols + 1

        if idx >= n or tokens[idx].kind != "WORD" or tokens[idx].upper != "AS":
            raise ReadOnlyViolationError("WITH CTE 语法缺少 AS 关键字")
        idx += 1

        # PG12+ 可选 [NOT] MATERIALIZED
        if idx < n and tokens[idx].kind == "WORD" and tokens[idx].upper == "NOT":
            idx += 1
        if idx < n and tokens[idx].kind == "WORD" and tokens[idx].upper == "MATERIALIZED":
            idx += 1

        if idx >= n or tokens[idx].kind != "LP":
            raise ReadOnlyViolationError("WITH CTE 定义体必须包裹在括号内")
        rp_body = _find_matching_rp(tokens, idx)
        body_tokens = tokens[idx + 1 : rp_body]
        if not body_tokens or body_tokens[0].kind != "WORD" or body_tokens[0].upper not in ("SELECT", "WITH"):
            raise ReadOnlyViolationError(
                "WITH CTE 内部仅允许嵌套 SELECT 查询，严禁数据修改 CTE"
            )
        if body_tokens[0].upper == "WITH":
            _validate_with_cte_structure(body_tokens)

        cte_count += 1
        idx = rp_body + 1
        if idx < n and tokens[idx].kind == "COMMA":
            idx += 1
            continue
        break

    if cte_count == 0 or idx >= n:
        raise ReadOnlyViolationError("WITH 语句缺少主查询体")
    if tokens[idx].kind != "WORD" or tokens[idx].upper != "SELECT":
        raise ReadOnlyViolationError(
            f"WITH 语句的主查询必须为 SELECT，严禁执行 [{tokens[idx].raw}]"
        )


def _validate_show_statement(tokens: List[SqlToken]) -> None:
    """校验 SHOW 仅用于安全库表元数据探查."""
    words: List[str] = []
    for tok in tokens[1:4]:
        if tok.kind == "WORD":
            words.append(tok.upper)
        else:
            break
    if not words:
        raise ReadOnlyViolationError("SHOW 语句缺少目标元数据对象类型")
    matched = False
    for allowed_prefix in _ALLOWED_SHOW_PREFIXES:
        k = len(allowed_prefix)
        if len(words) >= k and tuple(words[:k]) == allowed_prefix:
            matched = True
            break
    if not matched:
        raise ReadOnlyViolationError(
            f"只读沙箱禁止执行非库表结构类的 SHOW 语句: SHOW {' '.join(words[:2])}"
        )


def _validate_explain_statement(tokens: List[SqlToken]) -> None:
    """校验 EXPLAIN 不包含真实执行的 ANALYZE，且被解释的目标语句严格为只读查询."""
    for tok in tokens[1:]:
        if tok.kind == "WORD" and tok.upper == "ANALYZE":
            raise ReadOnlyViolationError(
                "只读沙箱严禁执行 EXPLAIN ANALYZE（该语法会真实运行目标语句）"
            )

    idx = 1
    n = len(tokens)
    # PostgreSQL EXPLAIN (FORMAT JSON, VERBOSE, ...) SELECT ...
    if idx < n and tokens[idx].kind == "LP":
        for opt_tok in tokens[idx + 1 : _find_matching_rp(tokens, idx)]:
            if opt_tok.kind == "WORD" and opt_tok.upper == "ANALYZE":
                raise ReadOnlyViolationError("只读沙箱严禁在 EXPLAIN 选项中开启 ANALYZE")
        idx = _find_matching_rp(tokens, idx) + 1

    # SQLite EXPLAIN QUERY PLAN / MySQL EXPLAIN EXTENDED|PARTITIONS|FORMAT=... / Oracle EXPLAIN PLAN FOR
    explain_modifiers = {
        "QUERY",
        "PLAN",
        "FOR",
        "EXTENDED",
        "PARTITIONS",
        "FORMAT",
        "TRADITIONAL",
        "JSON",
        "TREE",
        "VERBOSE",
        "COSTS",
    }
    while idx < n:
        t = tokens[idx]
        if t.kind == "WORD" and t.upper in explain_modifiers:
            idx += 1
            continue
        if t.kind == "OP" and t.raw == "=":
            idx += 1
            continue
        if t.kind == "STRING":
            idx += 1
            continue
        break

    sub_tokens = tokens[idx:]
    if not sub_tokens or sub_tokens[0].kind != "WORD" or sub_tokens[0].upper not in ("SELECT", "WITH"):
        raise ReadOnlyViolationError("EXPLAIN 仅允许解释纯只读的 SELECT / WITH 查询语句")
    _validate_token_stream(sub_tokens)


def _validate_token_stream(tokens: List[SqlToken]) -> None:
    """对单遍词法 Token 流执行单语句、顶层白名单、全语句禁写词与危险函数审计."""
    working = list(tokens)
    if working and working[-1].kind == "SEMI":
        working.pop()

    if not working:
        raise ReadOnlyViolationError("SQL 语句内容为空")

    # 1. 严禁多语句堆叠
    for tok in working:
        if tok.kind == "SEMI":
            raise ReadOnlyViolationError(
                "只读沙箱严禁多语句堆叠执行（检测到语句内部分号 ';'）"
            )

    # 2. 顶层首词必须在只读白名单内（且不得加引号伪装）
    first = working[0]
    if first.kind != "WORD" or first.upper not in _ALLOWED_TOP_VERBS:
        raise ReadOnlyViolationError(
            f"顶层动词 [{first.raw}] 不在只读白名单 ({'/'.join(sorted(_ALLOWED_TOP_VERBS))}) 内"
        )

    top_verb = first.upper
    if top_verb == "WITH":
        _validate_with_cte_structure(working)
    elif top_verb == "SHOW":
        _validate_show_statement(working)
    elif top_verb in ("DESCRIBE", "DESC"):
        if len(working) < 2 or working[1].kind not in ("WORD", "QIDENT"):
            raise ReadOnlyViolationError("DESCRIBE 语句缺少有效表名")
    elif top_verb == "EXPLAIN":
        _validate_explain_statement(working)
        return

    # 3. 全语句禁写关键字、行锁子句与危险系统函数/包扫描
    total = len(working)
    for idx, tok in enumerate(working):
        prev_tok = working[idx - 1] if idx > 0 else None
        next_tok = working[idx + 1] if idx + 1 < total else None

        # 3a. 危险系统函数调用拦截（同时覆盖未加引号与双引号/反引号包裹的函数名，如 "pg_sleep"(5)）
        if tok.kind in ("WORD", "QIDENT") and next_tok is not None and next_tok.kind == "LP":
            if tok.upper in _DANGEROUS_FUNCTIONS:
                raise ReadOnlyViolationError(
                    f"只读沙箱禁止调用高危或副作用系统函数: {tok.raw}()"
                )

        # 3b. Oracle/DM8/DB2 危险系统包调用拦截（如 DBMS_LOCK.SLEEP / UTL_HTTP.REQUEST）
        if tok.kind in ("WORD", "QIDENT") and next_tok is not None and next_tok.kind == "DOT":
            if tok.upper in _DANGEROUS_PACKAGES:
                raise ReadOnlyViolationError(
                    f"只读沙箱禁止访问高危系统程序包: {tok.raw}"
                )

        if tok.kind != "WORD":
            continue

        u = tok.upper

        # 3c. 无条件封禁的写/DDL/DCL/TCL/INTO 关键字
        if u in _BANNED_UNQUOTED_WORDS:
            raise ReadOnlyViolationError(
                f"只读沙箱严禁出现写操作或控制关键字: [{tok.raw}]"
            )

        # 3d. CREATE 仅允许出现在顶层 SHOW CREATE TABLE/VIEW 中
        if u == "CREATE":
            is_show_create = (
                top_verb == "SHOW"
                and idx == 1
                and next_tok is not None
                and next_tok.kind == "WORD"
                and next_tok.upper in ("TABLE", "VIEW")
            )
            if not is_show_create:
                raise ReadOnlyViolationError("只读沙箱严禁出现 CREATE 定义语句")

        # 3e. REPLACE / TRUNCATE 仅允许作为标量函数调用（紧跟左括号且不在语句开头）
        if u in ("REPLACE", "TRUNCATE"):
            if idx == 0 or next_tok is None or next_tok.kind != "LP":
                raise ReadOnlyViolationError(
                    f"只读沙箱严禁执行 [{tok.raw}] 写/清空表操作"
                )

        # 3f. SET 仅允许出现在 CAST(... CHARACTER SET xxx) 字符集声明中
        if u == "SET":
            if prev_tok is None or prev_tok.kind != "WORD" or prev_tok.upper != "CHARACTER":
                raise ReadOnlyViolationError("只读沙箱严禁执行 SET 会话修改或赋值操作")

        # 3g. FOR SHARE / FOR KEY SHARE 行级共享锁拦截（FOR UPDATE 已由 UPDATE 无条件拦截）
        if u == "FOR" and next_tok is not None and next_tok.kind == "WORD":
            if next_tok.upper == "SHARE":
                raise ReadOnlyViolationError("只读沙箱禁止使用 FOR SHARE 行级加锁子句")
            if (
                next_tok.upper == "KEY"
                and idx + 2 < total
                and working[idx + 2].kind == "WORD"
                and working[idx + 2].upper == "SHARE"
            ):
                raise ReadOnlyViolationError("只读沙箱禁止使用 FOR KEY SHARE 行级加锁子句")


def validate_readonly_sql(sql: str, db_type: str = "") -> str:
    """校验 SQL 是否 100% 满足只读白名单规范，返回去除首尾空白与末尾单个分号后的安全单条 SQL.

    若存在任何语法歧义、多语句、写/DDL/锁/INTO 或危险系统函数调用，立即抛出 `ReadOnlyViolationError`。
    """
    if not isinstance(sql, str):
        raise ReadOnlyViolationError("SQL 语句必须为字符串")
    if "\x00" in sql:
        raise ReadOnlyViolationError("SQL 语句包含非法空字节 (NUL)")

    cleaned = sql.strip()
    if not cleaned:
        raise ReadOnlyViolationError("要执行的只读 SQL 不能为空")

    dialect = (db_type or "").strip().upper()
    allow_pg_hash_op = dialect in ("PGSQL", "DWS", "KINGBASE")

    # 双模态词法扫描：ANSI 标准引号模式与反斜杠转义模式必须同时合法且同时通过全部只读规则校验
    tokens_ansi = _lex_sql(
        cleaned, backslash_escape=False, allow_pg_hash_op=allow_pg_hash_op
    )
    tokens_backslash = _lex_sql(
        cleaned, backslash_escape=True, allow_pg_hash_op=allow_pg_hash_op
    )

    _validate_token_stream(tokens_ansi)
    _validate_token_stream(tokens_backslash)

    # 剥离末尾单个分号及其后的尾注释，便于外层按需拼接安全 LIMIT
    result_sql = cleaned
    if tokens_ansi and tokens_ansi[-1].kind == "SEMI":
        result_sql = cleaned[: tokens_ansi[-1].pos].rstrip()
    return result_sql
