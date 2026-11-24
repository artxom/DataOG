"""Data.O.G 智能数据助手附件解析、语义分块、CSV 画像与内存只读查询引擎.

核心职责（零新增第三方依赖）：
1. 安全校验与解码：严格拦截超限体积（CSV <= 2MB / 纯文本与 SQL <= 5MB）、二进制空字节与不支持扩展名；
2. 语义感知分块（防爆窗基石）：
   - `.sql/.ddl`：优先按 `split_sql_statements` 完整语句边界打包，语句畸形时无缝降级按行切片，绝不超出单块硬阈值；
   - `.csv/.tsv`：自动推断分隔符、类型（保留前导零编号为 TEXT）并生成零 Token 成本统计画像，分块自动带表头；
   - `.txt/.md/.log/.json` 等：按标题/段落/换行边界分块，记录精确起止行号；
3. CSV `:memory:` SQLite 只读分析沙箱：先经 `validate_readonly_sql` 校验，建表灌入后立即锁 `PRAGMA query_only = ON` 与 3s/500k ops 熔断，支持精确分组聚合筛选。
"""

from __future__ import annotations

import bisect
from collections import Counter
import contextlib
import csv
import datetime
import io
import math
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from core.platform.readonly_guard import validate_readonly_sql
from core.platform.sql_risk_guard import split_sql_statements
from modules.ai_assistant.sql_static_analyzer import (
    analyze_sql_static,
    compact_sql_for_llm,
    format_static_analysis_brief,
)

TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".sql",
        ".ddl",
        ".md",
        ".log",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".ini",
        ".conf",
        ".py",
        ".sh",
    }
)
CSV_EXTENSIONS = frozenset({".csv", ".tsv"})
ALLOWED_ATTACHMENT_EXTENSIONS = TEXT_EXTENSIONS | CSV_EXTENSIONS

MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 5 * 1024 * 1024

INLINE_TOKEN_BUDGET = 32000
CSV_INLINE_TOKEN_BUDGET = 8000
CHUNK_TARGET_TOKENS = 12000
CHUNK_HARD_MAX_TOKENS = 16000
DEFAULT_BATCH_MAX_CHUNKS = 8
MAX_BATCH_CHUNKS_CAP = 12
MAX_ATTACHMENTS_PER_TURN = 5

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$")
_LEADING_WORD_RE = re.compile(r"^\s*(?:--[^\n]*\n|/\*[\s\S]*?\*/|\s)*([A-Za-z_]+)")
_MD_HEADING_RE = re.compile(r"^#{1,4}\s+(.+)$")
_SQL_HINT_RE = re.compile(
    r"(?is)\b(?:SELECT\b[\s\S]{1,200}\bFROM|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|INDEX|PROCEDURE|FUNCTION)|ALTER\s+TABLE|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO|DROP\s+TABLE|WITH\s+\w+\s+AS\s*\()"
)
_SQL_OBJECT_RE = re.compile(
    r"(?im)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*\S+\s+)?"
    r"(PROCEDURE|FUNCTION|PACKAGE\s+BODY|PACKAGE|TRIGGER|MATERIALIZED\s+VIEW|VIEW|TABLE)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_\"`\[\]][A-Za-z0-9_$.\"`\[\]]*)"
)
_SQL_BANNER_RE = re.compile(
    r"^\s*(?:--\s*(?:={2,}|-{3,}|#{1,4}|\*{2,})?\s*((?:Step|阶段|步骤|Part|第[一二三四五六七八九十0-9]+[步章节段]?|[0-9]+[\.、\)])\s*[^\n]{2,76}|[^\n=\-\*]{4,76})\s*(?:={2,}|-{3,}|\*{2,})?)\s*$"
)
_SQL_PROC_KEYWORD_RE = re.compile(
    r"(?im)^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*\S+\s+)?(?:PROCEDURE|FUNCTION|PACKAGE|TRIGGER)|DECLARE|BEGIN|EXCEPTION)\b"
)
_SQL_TABLE_REF_RE = re.compile(
    r"(?is)\b(?:FROM|JOIN|INTO|UPDATE|TRUNCATE\s+TABLE|DELETE\s+FROM|MERGE\s+INTO|CREATE\s+(?:OR\s+REPLACE\s+)?TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE)\s+"
    r"(?:ONLY\s+)?([A-Za-z_\"`\[\]][A-Za-z0-9_$.\"`\[\]]*)"
)
_SQL_REF_STOPWORDS = frozenset(
    {
        "SELECT",
        "WHERE",
        "SET",
        "VALUES",
        "DUAL",
        "LATERAL",
        "UNNEST",
        "TABLE",
        "ONLY",
        "AS",
        "ON",
        "AND",
        "OR",
        "NULL",
        "INTO",
        "FROM",
        "JOIN",
        "USING",
        "THEN",
        "ELSE",
        "END",
        "CASE",
        "WHEN",
        "IF",
        "LOOP",
        "BEGIN",
        "RETURN",
        "CALL",
        "EXEC",
        "EXECUTE",
        "CURSOR",
        "OPEN",
        "FETCH",
        "CLOSE",
        "ROWCOUNT",
        "FOUND",
        "NOTFOUND",
    }
)
_CODE_DEF_RE = re.compile(r"^\s*(?:async\s+def|def|class|function)\s+([A-Za-z_][A-Za-z0-9_]*)")
_LOG_ERR_RE = re.compile(r"(?i)\b(?:ERROR|FATAL|EXCEPTION|ORA-\d+|SQLSTATE|TRACEBACK)\b")


def get_attachment_limits() -> Dict[str, Any]:
    """返回前端展示与预检用的附件限制常量."""
    return {
        "max_csv_bytes": MAX_CSV_BYTES,
        "max_text_bytes": MAX_TEXT_BYTES,
        "inline_token_budget": INLINE_TOKEN_BUDGET,
        "csv_inline_token_budget": CSV_INLINE_TOKEN_BUDGET,
        "chunk_target_tokens": CHUNK_TARGET_TOKENS,
        "chunk_hard_max_tokens": CHUNK_HARD_MAX_TOKENS,
        "default_batch_max_chunks": DEFAULT_BATCH_MAX_CHUNKS,
        "max_attachments_per_turn": MAX_ATTACHMENTS_PER_TURN,
        "allowed_extensions": sorted(ALLOWED_ATTACHMENT_EXTENSIONS),
        "csv_extensions": sorted(CSV_EXTENSIONS),
        "text_extensions": sorted(TEXT_EXTENSIONS),
    }


def format_with_line_numbers(text: str, start_line: int = 1) -> str:
    """为文本或代码添加紧凑行号前缀 (`L| line`)，便于大模型精确引用位置."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(f"{start_line + idx}| {ln}" for idx, ln in enumerate(lines))


def extract_attachment_outline(text: str, file_kind: str) -> Dict[str, Any]:
    """提取附件零 Token 成本结构化大纲（存储过程名、逻辑章节行号、涉及表及代码符号）."""
    if not text or file_kind == "csv":
        return {
            "is_stored_procedure": False,
            "objects": [],
            "sections": [],
            "referenced_tables": [],
            "summary_line": "",
        }

    lines = text.splitlines()
    objects: List[Dict[str, Any]] = []
    sections: List[Dict[str, Any]] = []

    if file_kind == "sql":
        has_proc_ddl = False
        has_begin = False
        has_proc_body_kw = False

        for line_no, raw_ln in enumerate(lines, start=1):
            stripped = raw_ln.strip()
            if not stripped:
                continue
            upper_s = stripped.upper()

            obj_m = _SQL_OBJECT_RE.match(raw_ln)
            if obj_m:
                kind = re.sub(r"\s+", " ", obj_m.group(1).upper())
                name = obj_m.group(2).strip().strip('"`[]')
                objects.append({"kind": kind, "name": name, "line": line_no})
                if kind in ("PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY", "TRIGGER"):
                    has_proc_ddl = True
                sections.append({"line": line_no, "title": f"CREATE {kind} {name}"[:80], "kind": "object"})
                continue

            if re.match(r"^(?:BEGIN)\b", upper_s):
                has_begin = True
                if len(sections) < 60:
                    sections.append({"line": line_no, "title": "BEGIN 主体块", "kind": "block"})
                continue
            if re.match(r"^(?:DECLARE|EXCEPTION)\b", upper_s):
                has_proc_body_kw = True
                kw = upper_s.split()[0]
                if len(sections) < 60:
                    sections.append({"line": line_no, "title": f"{kw} 声明/异常处理段", "kind": "block"})
                continue
            if re.search(r"\b(?:CURSOR\s+\w+\s+IS|FOR\s+\w+\s+IN|WHILE\s+.*\s+LOOP|END\s+LOOP|ELSIF)\b", upper_s):
                has_proc_body_kw = True

            if stripped.startswith("--") and len(sections) < 60:
                bm = _SQL_BANNER_RE.match(raw_ln)
                is_strong_comment = (
                    "===" in stripped
                    or "---" in stripped
                    or "***" in stripped
                    or bool(
                        re.match(
                            r"^--\s*(?:Step|阶段|步骤|Part|第[一二三四五六七八九十0-9]+|[0-9]+[\.、\)])",
                            stripped,
                            re.IGNORECASE,
                        )
                    )
                )
                if bm and is_strong_comment:
                    title_txt = re.sub(r"^[\-\=\*#\s]+|[\-\=\*#\s]+$", "", bm.group(1)).strip()
                    if len(title_txt) >= 2:
                        sections.append({"line": line_no, "title": title_txt[:80], "kind": "section"})

        # 剥离单引号字符串与行注释后提取涉及表名
        cleaned_for_tables = re.sub(r"'(?:''|[^'])*'", "''", text)
        cleaned_for_tables = re.sub(r"--[^\n]*", "", cleaned_for_tables)
        ref_tables: List[str] = []
        seen_tables: set[str] = set()
        for tm in _SQL_TABLE_REF_RE.finditer(cleaned_for_tables):
            raw_tbl = tm.group(1).strip().strip('"`[]').rstrip(",;")
            if not raw_tbl or raw_tbl.startswith((":", "@", "&", "$")):
                continue
            upper_t = raw_tbl.upper()
            if upper_t in _SQL_REF_STOPWORDS or len(raw_tbl) < 2:
                continue
            if "." in raw_tbl:
                parts = [p.strip('"`[]') for p in raw_tbl.split(".") if p.strip('"`[]')]
                raw_tbl = ".".join(parts)
            low_k = raw_tbl.lower()
            if low_k not in seen_tables:
                seen_tables.add(low_k)
                ref_tables.append(raw_tbl)
            if len(ref_tables) >= 30:
                break

        is_proc = bool(has_proc_ddl or (has_begin and has_proc_body_kw))
        obj_desc = "、".join(f"{o['kind']} {o['name']}" for o in objects[:4])
        parts_sum: List[str] = []
        if obj_desc:
            parts_sum.append(obj_desc)
        elif is_proc:
            parts_sum.append("存储过程/PL-SQL 代码块")
        if sections:
            parts_sum.append(f"{len(sections)} 个结构锚点")
        if ref_tables:
            parts_sum.append(f"涉及 {len(ref_tables)} 张表 ({', '.join(ref_tables[:5])}{'…' if len(ref_tables) > 5 else ''})")
        return {
            "is_stored_procedure": is_proc,
            "objects": objects[:25],
            "sections": sections[:60],
            "referenced_tables": ref_tables,
            "summary_line": " | ".join(parts_sum),
        }

    # 非 SQL 纯文本/Markdown/代码/日志大纲
    err_lines = 0
    for line_no, raw_ln in enumerate(lines, start=1):
        stripped = raw_ln.strip()
        if not stripped:
            continue
        hm = _MD_HEADING_RE.match(stripped)
        if hm and len(sections) < 60:
            sections.append({"line": line_no, "title": hm.group(1).strip()[:80], "kind": "heading"})
            continue
        cm = _CODE_DEF_RE.match(raw_ln)
        if cm and len(objects) < 30:
            objects.append({"kind": "SYMBOL", "name": cm.group(1), "line": line_no})
            sections.append({"line": line_no, "title": stripped[:80], "kind": "symbol"})
            continue
        if _LOG_ERR_RE.search(stripped):
            err_lines += 1
            if len(sections) < 40:
                sections.append({"line": line_no, "title": _truncate_line_preview(stripped, 64), "kind": "log_alert"})

    summary_parts: List[str] = []
    if objects:
        summary_parts.append(f"{len(objects)} 个符号定义")
    if sections:
        summary_parts.append(f"{len(sections)} 个章节/定位点")
    if err_lines:
        summary_parts.append(f"{err_lines} 行异常/报错信号")
    return {
        "is_stored_procedure": False,
        "objects": objects,
        "sections": sections[:60],
        "referenced_tables": [],
        "summary_line": " | ".join(summary_parts),
    }


def estimate_tokens(text: str) -> int:
    """保守估算文本 Token 数（O(N) 零大对象分配：中文/宽字符约 1.5 tok/字，ASCII 约 0.28 tok/字符）."""
    if not text:
        return 0
    char_len = len(text)
    utf8_len = len(text.encode("utf-8", errors="ignore"))
    wide_approx = max(0, min(char_len, (utf8_len - char_len) // 2))
    ascii_approx = max(0, char_len - wide_approx)
    return max(1, math.ceil(wide_approx * 1.5 + ascii_approx * 0.28))


def _truncate_line_preview(text: str, max_len: int = 44) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    return clean if len(clean) <= max_len else clean[: max_len - 1] + "…"


def infer_pasted_filename_and_ext(
    content: str, *, filename: str = "", file_hint: str = ""
) -> Tuple[str, str]:
    """为剪贴板粘贴文本推导安全的默认文件名与扩展名."""
    raw_name = os.path.basename((filename or "").replace("\\", "/").strip())
    if raw_name:
        ext = os.path.splitext(raw_name)[1].lower()
        if ext in ALLOWED_ATTACHMENT_EXTENSIONS:
            return raw_name[:120], ext

    stamp = datetime.datetime.now().strftime("%H%M%S")
    hint = (file_hint or "").strip().lower()
    if hint in ("sql", ".sql"):
        return f"pasted_sql_{stamp}.sql", ".sql"
    if hint in ("csv", ".csv"):
        return f"pasted_data_{stamp}.csv", ".csv"
    if hint in ("tsv", ".tsv"):
        return f"pasted_data_{stamp}.tsv", ".tsv"
    if hint in ("txt", ".txt", "text"):
        return f"pasted_text_{stamp}.txt", ".txt"

    sample = (content or "")[:6000]
    if _SQL_HINT_RE.search(sample):
        return f"pasted_sql_{stamp}.sql", ".sql"

    lines = [ln for ln in sample.splitlines()[:10] if ln.strip()]
    if len(lines) >= 2:
        for sep, ext, prefix in ((",", ".csv", "pasted_data"), ("\t", ".tsv", "pasted_data")):
            counts = [ln.count(sep) for ln in lines[:5]]
            if counts[0] >= 1 and all(c == counts[0] for c in counts):
                return f"{prefix}_{stamp}{ext}", ext

    return f"pasted_text_{stamp}.txt", ".txt"


def validate_and_decode_attachment(
    filename: str, raw_bytes: bytes
) -> Tuple[str, str, str, str, str]:
    """安全校验附件扩展名、体积与空字节，并返回 (safe_name, ext, decoded_text, encoding, file_kind)."""
    safe_name = os.path.basename((filename or "").replace("\\", "/").strip())
    if not safe_name:
        raise ValueError("文件名不能为空")
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise ValueError(
            f"暂不支持文件类型 `{ext or '无后缀'}`，请上传纯文本/SQL（.txt/.sql/.ddl/.md/.log/.json 等）或 .csv/.tsv 文件"
        )
    if not raw_bytes:
        raise ValueError("附件内容为空，请确认文件包含有效内容")

    if ext in CSV_EXTENSIONS and len(raw_bytes) > MAX_CSV_BYTES:
        mb = len(raw_bytes) / (1024 * 1024)
        raise ValueError(
            f"CSV/TSV 文件 `{safe_name}` 体积为 {mb:.2f} MB，超出 {MAX_CSV_BYTES // (1024 * 1024)} MB 限制，请筛选抽样后上传"
        )
    if ext in TEXT_EXTENSIONS and len(raw_bytes) > MAX_TEXT_BYTES:
        mb = len(raw_bytes) / (1024 * 1024)
        raise ValueError(
            f"文本/SQL 文件 `{safe_name}` 体积为 {mb:.2f} MB，超出 {MAX_TEXT_BYTES // (1024 * 1024)} MB 限制，请拆分为较小文件后上传"
        )

    if b"\x00" in raw_bytes[:8192]:
        raise ValueError(f"文件 `{safe_name}` 疑似二进制文件（含 NUL 空字节），仅支持纯文本与 CSV")

    decoded = ""
    detected_enc = "utf-8"
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            decoded = raw_bytes.decode(enc)
            detected_enc = enc
            break
        except UnicodeDecodeError:
            continue

    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise ValueError(f"文件 `{safe_name}` 内容全为空白字符，无法解析")

    file_kind = "sql" if ext in (".sql", ".ddl") else ("csv" if ext in CSV_EXTENSIONS else "text")
    return safe_name[:160], ext, normalized, detected_enc, file_kind


def _slice_char_prefix_within_tokens(text: str, max_tokens: int) -> int:
    """二分查找不超过 max_tokens 的最大字符前缀长度（保证至少切出 1 字符）."""
    if not text or estimate_tokens(text) <= max_tokens:
        return len(text)
    lo, hi = 1, len(text)
    best = 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    # 尽量回退到最近换行符避免截断行中间
    nl = text.rfind("\n", max(1, best // 2), best + 1)
    if nl > 0 and estimate_tokens(text[:nl]) <= max_tokens:
        return nl + 1
    return best


def _force_split_text_to_budget(
    piece: str, *, hard_max_tokens: int = CHUNK_HARD_MAX_TOKENS
) -> List[str]:
    """确保任意超长文本片均被安全切分为严格 <= hard_max_tokens 的子块."""
    if estimate_tokens(piece) <= hard_max_tokens:
        return [piece]
    parts: List[str] = []
    rest = piece
    while rest:
        cut = _slice_char_prefix_within_tokens(rest, hard_max_tokens)
        sub = rest[:cut]
        if not sub:
            sub = rest[:1]
            cut = 1
        parts.append(sub)
        rest = rest[cut:]
    return parts


def _build_line_offsets(text: str) -> List[int]:
    offsets = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            offsets.append(idx + 1)
    return offsets


def _offset_to_line(offsets: List[int], char_pos: int) -> int:
    return bisect.bisect_right(offsets, max(0, char_pos))


def _chunk_plain_text(text: str, *, label_prefix: str = "文本片段") -> List[Dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return []

    raw_chunks: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_tokens = 0
    chunk_start_line = 1
    first_heading = ""

    def _flush(end_line_no: int) -> None:
        nonlocal buf, buf_tokens, first_heading
        if not buf:
            return
        joined = "".join(buf)
        sub_pieces = _force_split_text_to_budget(joined, hard_max_tokens=CHUNK_HARD_MAX_TOKENS)
        if len(sub_pieces) == 1:
            preview = first_heading or _truncate_line_preview(
                next((ln for ln in buf if ln.strip()), "")
            )
            title = f"{label_prefix} · {preview}" if preview else f"{label_prefix}"
            raw_chunks.append(
                {
                    "title": title[:80],
                    "range_label": f"L{chunk_start_line}-L{end_line_no}",
                    "start_line": chunk_start_line,
                    "end_line": end_line_no,
                    "estimated_tokens": estimate_tokens(joined),
                    "content": joined,
                }
            )
        else:
            cur_line = chunk_start_line
            for s_idx, sp in enumerate(sub_pieces, start=1):
                sp_lines = sp.count("\n")
                sp_end = min(end_line_no, cur_line + max(0, sp_lines))
                raw_chunks.append(
                    {
                        "title": f"{label_prefix} [子段 {s_idx}/{len(sub_pieces)}]"[:80],
                        "range_label": f"L{cur_line}-L{sp_end}",
                        "start_line": cur_line,
                        "end_line": sp_end,
                        "estimated_tokens": estimate_tokens(sp),
                        "content": sp,
                    }
                )
                cur_line = sp_end + (1 if sp.endswith("\n") and sp_end < end_line_no else 0)
        buf = []
        buf_tokens = 0
        first_heading = ""

    for line_no, ln in enumerate(lines, start=1):
        ln_tokens = estimate_tokens(ln)
        hm = _MD_HEADING_RE.match(ln.strip())
        # 在 Markdown 标题或空行处适度提前切分（当已累积 >= 65% 目标容量）
        if buf and (
            (buf_tokens + ln_tokens > CHUNK_TARGET_TOKENS)
            or (hm and buf_tokens >= int(CHUNK_TARGET_TOKENS * 0.55))
            or (not ln.strip() and buf_tokens >= int(CHUNK_TARGET_TOKENS * 0.8))
        ):
            _flush(line_no - 1)
            chunk_start_line = line_no
        if hm and not first_heading:
            first_heading = hm.group(1).strip()
        buf.append(ln)
        buf_tokens += ln_tokens

    _flush(len(lines))
    for i, ch in enumerate(raw_chunks):
        ch["chunk_index"] = i
    return raw_chunks


def _chunk_stored_procedure_lines(
    text: str, outline: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """按存储过程逻辑区块边界聚合切分为大语义段 (~12K tokens/段)，避免被分号拆成海量碎片."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return []

    section_by_line: Dict[int, str] = {}
    for sec in outline.get("sections") or []:
        ln = int(sec.get("line") or 0)
        title = str(sec.get("title") or "").strip()
        if ln > 0 and title and ln not in section_by_line:
            section_by_line[ln] = title

    raw_chunks: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_tokens = 0
    chunk_start_line = 1
    first_label = ""

    def _flush(end_line_no: int) -> None:
        nonlocal buf, buf_tokens, first_label
        if not buf:
            return
        joined = "".join(buf)
        sub_pieces = _force_split_text_to_budget(joined, hard_max_tokens=CHUNK_HARD_MAX_TOKENS)
        label = first_label or _truncate_line_preview(
            next((ln for ln in buf if ln.strip()), ""), 36
        )
        if len(sub_pieces) == 1:
            raw_chunks.append(
                {
                    "title": f"过程段 · {label}"[:80] if label else "存储过程片段",
                    "range_label": f"L{chunk_start_line}-L{end_line_no}",
                    "start_line": chunk_start_line,
                    "end_line": end_line_no,
                    "estimated_tokens": estimate_tokens(joined),
                    "content": joined,
                }
            )
        else:
            cur_line = chunk_start_line
            for s_idx, sp in enumerate(sub_pieces, start=1):
                sp_lines = sp.count("\n")
                sp_end = min(end_line_no, cur_line + max(0, sp_lines))
                raw_chunks.append(
                    {
                        "title": f"过程段 · {label} [子段 {s_idx}/{len(sub_pieces)}]"[:80],
                        "range_label": f"L{cur_line}-L{sp_end}",
                        "start_line": cur_line,
                        "end_line": sp_end,
                        "estimated_tokens": estimate_tokens(sp),
                        "content": sp,
                    }
                )
                cur_line = sp_end + (1 if sp.endswith("\n") and sp_end < end_line_no else 0)
        buf = []
        buf_tokens = 0
        first_label = ""

    for line_no, ln in enumerate(lines, start=1):
        ln_tokens = estimate_tokens(ln)
        sec_title = section_by_line.get(line_no, "")
        if buf and (
            (buf_tokens + ln_tokens > CHUNK_TARGET_TOKENS)
            or (sec_title and buf_tokens >= int(CHUNK_TARGET_TOKENS * 0.55))
            or (not ln.strip() and buf_tokens >= int(CHUNK_TARGET_TOKENS * 0.80))
        ):
            _flush(line_no - 1)
            chunk_start_line = line_no
        if sec_title and not first_label:
            first_label = sec_title
        buf.append(ln)
        buf_tokens += ln_tokens

    _flush(len(lines))
    for i, ch in enumerate(raw_chunks):
        ch["chunk_index"] = i
    return raw_chunks


def _chunk_sql_text(
    text: str, *, outline: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    eff_outline = outline if isinstance(outline, dict) else extract_attachment_outline(text, "sql")
    if eff_outline.get("is_stored_procedure"):
        return _chunk_stored_procedure_lines(text, eff_outline)

    try:
        stmts = split_sql_statements(text, max_statements=5000)
    except Exception:
        return _chunk_plain_text(text, label_prefix="SQL 片段")

    if not stmts:
        return _chunk_plain_text(text, label_prefix="SQL 片段")

    line_offsets = _build_line_offsets(text)
    cursor = 0
    located_stmts: List[Tuple[int, str, str, int, int]] = []
    for idx, stmt in enumerate(stmts, start=1):
        pos = text.find(stmt, cursor)
        if pos < 0:
            pos = cursor
        end_pos = min(len(text), pos + len(stmt))
        cursor = end_pos
        s_line = _offset_to_line(line_offsets, pos)
        e_line = _offset_to_line(line_offsets, max(pos, end_pos - 1))
        vm = _LEADING_WORD_RE.match(stmt)
        verb = vm.group(1).upper() if vm else "SQL"
        stmt_with_semi = stmt if stmt.rstrip().endswith(";") else f"{stmt};"
        located_stmts.append((idx, verb, stmt_with_semi, s_line, e_line))

    raw_chunks: List[Dict[str, Any]] = []
    grp_items: List[Tuple[int, str, str, int, int]] = []
    grp_tokens = 0

    def _flush_sql_group() -> None:
        nonlocal grp_items, grp_tokens
        if not grp_items:
            return
        first_idx, _, _, g_start, _ = grp_items[0]
        last_idx, _, _, _, g_end = grp_items[-1]
        verbs: List[str] = []
        for _, v, _, _, _ in grp_items:
            if v not in verbs:
                verbs.append(v)
        verbs_tag = "/".join(verbs[:3])
        idx_tag = f"#{first_idx}" if first_idx == last_idx else f"#{first_idx}~#{last_idx}"
        joined = "\n\n".join(it[2] for it in grp_items)
        pieces = _force_split_text_to_budget(joined, hard_max_tokens=CHUNK_HARD_MAX_TOKENS)
        if len(pieces) == 1:
            raw_chunks.append(
                {
                    "title": f"语句 {idx_tag} ({verbs_tag})"[:80],
                    "range_label": f"L{g_start}-L{g_end}",
                    "start_line": g_start,
                    "end_line": g_end,
                    "estimated_tokens": estimate_tokens(joined),
                    "content": joined,
                }
            )
        else:
            for p_idx, piece in enumerate(pieces, start=1):
                raw_chunks.append(
                    {
                        "title": f"语句 {idx_tag} ({verbs_tag}) [子段 {p_idx}/{len(pieces)}]"[:80],
                        "range_label": f"L{g_start}-L{g_end} · {p_idx}/{len(pieces)}",
                        "start_line": g_start,
                        "end_line": g_end,
                        "estimated_tokens": estimate_tokens(piece),
                        "content": piece,
                    }
                )
        grp_items = []
        grp_tokens = 0

    for item in located_stmts:
        item_tokens = estimate_tokens(item[2])
        if grp_items and grp_tokens + item_tokens > CHUNK_TARGET_TOKENS:
            _flush_sql_group()
        grp_items.append(item)
        grp_tokens += item_tokens

    _flush_sql_group()
    for i, ch in enumerate(raw_chunks):
        ch["chunk_index"] = i
    return raw_chunks


def _detect_csv_delimiter(text: str, ext: str) -> str:
    sample = text[:8192]
    if ext == ".tsv" and "\t" in sample:
        return "\t"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        if dialect.delimiter:
            return dialect.delimiter
    except Exception:
        pass
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    candidates = ("\t", ",", ";", "|")
    best = "\t" if ext == ".tsv" else ","
    best_cnt = first_line.count(best)
    for d in candidates:
        c = first_line.count(d)
        if c > best_cnt:
            best_cnt = c
            best = d
    return best


def _sanitize_csv_headers(raw_headers: List[str]) -> List[str]:
    seen_lower: set[str] = set()
    result: List[str] = []
    for idx, h in enumerate(raw_headers):
        base = re.sub(r"[\r\n\t]+", " ", (h or "").lstrip("\ufeff").strip())
        if not base:
            base = f"col_{idx + 1}"
        cand = base[:80]
        suffix = 2
        while cand.lower() in seen_lower:
            cand = f"{base[:72]}_{suffix}"
            suffix += 1
        seen_lower.add(cand.lower())
        result.append(cand)
    return result


def _is_clean_integer(s: str) -> bool:
    if not _INT_RE.match(s):
        return False
    digits = s[1:] if s[0] in "+-" else s
    # 关键金融/数仓防护：带前导零的多位数字串（如 001024、010）必须保留为 TEXT！
    if len(digits) > 1 and digits.startswith("0"):
        return False
    try:
        val = int(s)
        return -(2**63) <= val <= (2**63 - 1)
    except Exception:
        return False


def _infer_column_type(values: List[str]) -> str:
    non_empty = [v.strip() for v in values if v is not None and v.strip() != ""]
    if not non_empty:
        return "TEXT"
    if all(_is_clean_integer(v) for v in non_empty):
        return "INTEGER"
    for v in non_empty:
        if not (_is_clean_integer(v) or _FLOAT_RE.match(v)):
            return "TEXT"
        try:
            fval = float(v)
            if not math.isfinite(fval):
                return "TEXT"
        except Exception:
            return "TEXT"
    return "REAL"


def _serialize_csv_row(row: List[str], delimiter: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="")
    writer.writerow(row)
    return buf.getvalue()


def _parse_and_profile_csv(
    text: str, ext: str
) -> Tuple[str, List[str], List[List[str]], Dict[str, Any]]:
    delimiter = _detect_csv_delimiter(text, ext)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    all_rows = [list(r) for r in reader]
    # 剔除完全空白的尾部或分隔空行
    non_empty_rows = [r for r in all_rows if r and any(str(c or "").strip() != "" for c in r)]
    if not non_empty_rows:
        raise ValueError("CSV/TSV 文件中未解析到有效表头与数据行")

    raw_header = list(non_empty_rows[0])
    data_rows = non_empty_rows[1:]
    max_width = max([len(raw_header)] + [len(r) for r in data_rows])
    while len(raw_header) < max_width:
        raw_header.append(f"col_{len(raw_header) + 1}")
    headers = _sanitize_csv_headers(raw_header)

    normalized_rows: List[List[str]] = []
    for r in data_rows:
        padded = list(r[:max_width])
        if len(padded) < max_width:
            padded.extend([""] * (max_width - len(padded)))
        normalized_rows.append(padded)

    col_profiles: List[Dict[str, Any]] = []
    for c_idx, col_name in enumerate(headers):
        col_vals = [row[c_idx] for row in normalized_rows]
        inferred = _infer_column_type(col_vals)
        non_empty_vals = [v.strip() for v in col_vals if v is not None and v.strip() != ""]
        null_cnt = len(col_vals) - len(non_empty_vals)
        counter = Counter(non_empty_vals)
        prof_item: Dict[str, Any] = {
            "name": col_name,
            "inferred_type": inferred,
            "non_null_count": len(non_empty_vals),
            "null_count": null_cnt,
            "distinct_count": len(counter),
            "top_values": [
                {"value": k[:60], "count": cnt} for k, cnt in counter.most_common(5)
            ],
        }
        if inferred in ("INTEGER", "REAL") and non_empty_vals:
            nums = [float(v) for v in non_empty_vals]
            min_v = min(nums)
            max_v = max(nums)
            mean_v = sum(nums) / len(nums)
            prof_item["min"] = int(min_v) if inferred == "INTEGER" else round(min_v, 4)
            prof_item["max"] = int(max_v) if inferred == "INTEGER" else round(max_v, 4)
            prof_item["mean"] = round(mean_v, 4)
        col_profiles.append(prof_item)

    sample_rows = [
        [str(cell)[:120] for cell in r] for r in normalized_rows[:8]
    ]
    profile: Dict[str, Any] = {
        "delimiter": "\\t" if delimiter == "\t" else delimiter,
        "row_count": len(normalized_rows),
        "column_count": len(headers),
        "columns": col_profiles,
        "sample_rows": sample_rows,
    }
    return delimiter, headers, normalized_rows, profile


def _chunk_csv_rows(
    delimiter: str,
    headers: List[str],
    data_rows: List[List[str]],
) -> List[Dict[str, Any]]:
    header_line = _serialize_csv_row(headers, delimiter)
    header_tokens = estimate_tokens(header_line + "\n")
    if not data_rows:
        return [
            {
                "chunk_index": 0,
                "title": "表头定义 (0 行数据)",
                "range_label": "Header",
                "start_line": 1,
                "end_line": 1,
                "estimated_tokens": max(1, header_tokens),
                "content": header_line,
            }
        ]

    raw_chunks: List[Dict[str, Any]] = []
    cur_lines: List[str] = []
    cur_tokens = header_tokens
    start_row = 1

    def _flush_csv_batch(end_row_no: int) -> None:
        nonlocal cur_lines, cur_tokens
        if not cur_lines:
            return
        full_block = header_line + "\n" + "\n".join(cur_lines)
        pieces = _force_split_text_to_budget(full_block, hard_max_tokens=CHUNK_HARD_MAX_TOKENS)
        for p_idx, piece in enumerate(pieces, start=1):
            suffix = f" [子段 {p_idx}/{len(pieces)}]" if len(pieces) > 1 else ""
            raw_chunks.append(
                {
                    "title": f"数据行 #{start_row}~#{end_row_no}{suffix}"[:80],
                    "range_label": f"Row {start_row}-{end_row_no}"
                    + (f" ({p_idx}/{len(pieces)})" if len(pieces) > 1 else ""),
                    "start_line": start_row + 1,
                    "end_line": end_row_no + 1,
                    "estimated_tokens": estimate_tokens(piece),
                    "content": piece,
                }
            )
        cur_lines = []
        cur_tokens = header_tokens

    for r_idx, row in enumerate(data_rows, start=1):
        row_line = _serialize_csv_row(row, delimiter)
        row_tokens = estimate_tokens(row_line + "\n")
        if cur_lines and cur_tokens + row_tokens > CHUNK_TARGET_TOKENS:
            _flush_csv_batch(r_idx - 1)
            start_row = r_idx
        cur_lines.append(row_line)
        cur_tokens += row_tokens

    _flush_csv_batch(len(data_rows))
    for i, ch in enumerate(raw_chunks):
        ch["chunk_index"] = i
    return raw_chunks


def recommend_strategy(
    file_kind: str,
    estimated_tokens: int,
    *,
    inline_budget: int = INLINE_TOKEN_BUDGET,
) -> str:
    if file_kind == "csv":
        return "full_context" if estimated_tokens <= CSV_INLINE_TOKEN_BUDGET else "agentic_explore"
    return "full_context" if estimated_tokens <= inline_budget else "agentic_explore"


def process_attachment_payload(
    filename: str, raw_bytes: bytes, *, dialect: str = ""
) -> Dict[str, Any]:
    """统一入口：校验、解码、计算 Token、提取结构大纲、静态 SQL 解剖、生成 CSV 画像与大粒度语义分块."""
    safe_name, ext, text, enc, file_kind = validate_and_decode_attachment(filename, raw_bytes)
    lines_cnt = len(text.splitlines())
    est_tokens = estimate_tokens(text)
    outline = extract_attachment_outline(text, file_kind)
    analysis: Dict[str, Any] = {}

    row_count = 0
    csv_profile: Dict[str, Any] = {}
    if file_kind == "csv":
        delimiter, headers, data_rows, csv_profile = _parse_and_profile_csv(text, ext)
        row_count = len(data_rows)
        chunks = _chunk_csv_rows(delimiter, headers, data_rows)
        summary = (
            f"CSV 数据表：{row_count} 行 × {len(headers)} 列 | 估算 ~{est_tokens} tokens | 切分为 {len(chunks)} 片"
        )
        eff_budget = CSV_INLINE_TOKEN_BUDGET
    elif file_kind == "sql":
        chunks = _chunk_sql_text(text, outline=outline)
        try:
            analysis = analyze_sql_static(text, dialect=dialect)
        except Exception:
            analysis = {}
        kind_lbl = "存储过程/SQL" if outline.get("is_stored_procedure") else "SQL 脚本"
        fc = analysis.get("finding_counts") or {}
        hm_cnt = int(fc.get("HIGH", 0)) + int(fc.get("MEDIUM", 0))
        risk_tag = f" | 静态疑点 {hm_cnt} 项" if hm_cnt > 0 else ""
        summary = f"{kind_lbl}：{lines_cnt} 行 | 估算 ~{est_tokens} tokens | {len(chunks)} 个语义段{risk_tag}"
        eff_budget = INLINE_TOKEN_BUDGET
    else:
        chunks = _chunk_plain_text(text, label_prefix="文本段落")
        summary = f"纯文本文档：{lines_cnt} 行 | 估算 ~{est_tokens} tokens | {len(chunks)} 段"
        eff_budget = INLINE_TOKEN_BUDGET

    oversized = est_tokens > eff_budget
    strategy = recommend_strategy(file_kind, est_tokens, inline_budget=eff_budget)
    return {
        "filename": safe_name,
        "file_ext": ext,
        "file_kind": file_kind,
        "encoding": enc,
        "size_bytes": len(raw_bytes),
        "line_count": lines_cnt,
        "row_count": row_count,
        "estimated_tokens": est_tokens,
        "chunk_count": len(chunks),
        "oversized": oversized,
        "recommended_strategy": strategy,
        "summary": summary,
        "raw_text": text,
        "csv_profile": csv_profile,
        "outline": outline,
        "analysis": analysis,
        "chunks": chunks,
    }


def format_attachment_manifest(att: Dict[str, Any], *, compact_mode: bool = False) -> str:
    """生成极致紧凑的文件总览地图（含存储过程结构大纲与静态血缘图纸），供 Agent 按需精准定位阅读."""
    lines = [
        f"附件 `{att.get('filename')}` [id={att.get('attachment_id')}, 类型={att.get('file_kind')}, "
        f"{att.get('line_count', 0)} 行, ~{att.get('estimated_tokens', 0)} tokens, 共 {att.get('chunk_count', 0)} 段]"
    ]
    outline = att.get("outline")
    if isinstance(outline, dict):
        if outline.get("summary_line"):
            lines.append(f"  结构大纲: {outline['summary_line']}")
        objs = outline.get("objects") or []
        if objs:
            obj_str = "；".join(f"{o.get('kind')} {o.get('name')} (L{o.get('line')})" for o in objs[:8])
            lines.append(f"  主要对象: {obj_str}")
        secs = outline.get("sections") or []
        sec_cap = 12 if compact_mode else 20
        if secs:
            lines.append("  逻辑锚点（可用 `read_attachment_lines` 指定 start_line/end_line 精读）:")
            for sec in secs[:sec_cap]:
                lines.append(f"    - L{sec.get('line')}: {sec.get('title')}")
            if len(secs) > sec_cap:
                lines.append(f"    - ... 其余 {len(secs) - sec_cap} 个锚点可通过 `search_attachment` 搜索")
        refs = outline.get("referenced_tables") or []
        if refs and not (isinstance(att.get("analysis"), dict) and (att["analysis"].get("lineage_dag") or {}).get("nodes")):
            lines.append(f"  涉及表清单: {', '.join(refs[:24])}")

    analysis = att.get("analysis")
    if isinstance(analysis, dict) and analysis.get("statements"):
        brief = format_static_analysis_brief(
            analysis,
            max_lineage_lines=8 if compact_mode else 12,
            max_findings=10 if compact_mode else 14,
            max_statements=14 if compact_mode else 22,
        )
        if brief:
            lines.append(brief)

    if att.get("file_kind") == "csv" and isinstance(att.get("csv_profile"), dict):
        prof = att["csv_profile"]
        cols = prof.get("columns") or []
        lines.append(
            f"  CSV 画像：共 {prof.get('row_count', 0)} 行 × {prof.get('column_count', 0)} 列"
            "（可使用 `query_csv_attachment` 将表名写作 `csv_data` 执行精确 SQL 查询）"
        )
        col_descs: List[str] = []
        for c in cols[:24]:
            extra = ""
            if "min" in c and "max" in c:
                extra = f", min={c['min']}, max={c['max']}, avg={c.get('mean')}"
            elif c.get("top_values"):
                top_s = "/".join(f"{tv['value']}({tv['count']})" for tv in c["top_values"][:3])
                extra = f", top=[{top_s}]"
            col_descs.append(
                f"    - `{c['name']}` ({c['inferred_type']}, 非空={c['non_null_count']}, 唯一={c['distinct_count']}{extra})"
            )
        if len(cols) > 24:
            col_descs.append(f"    - ... 其余 {len(cols) - 24} 列略")
        lines.extend(col_descs)
        sample_rows = prof.get("sample_rows") or []
        if sample_rows and cols:
            hdr_names = [c["name"] for c in cols]
            lines.append("  前几行样本: " + " | ".join(hdr_names[:10]))
            for sr in sample_rows[:3]:
                lines.append("    " + " | ".join(str(v) for v in sr[:10]))

    chunks = att.get("chunks") or []
    if chunks:
        lines.append("  语义分片目录（可用 `read_attachment_lines` 或 `inspect_attachment_chunk` 读取）:")
        for ch in chunks[:16]:
            lines.append(
                f"    - chunk_index={ch['chunk_index']}: {ch['title']} [{ch['range_label']}] (~{ch['estimated_tokens']} tok)"
            )
        if len(chunks) > 16:
            lines.append(f"    - ... 共 {len(chunks)} 段，可通过 `search_attachment` 检索定位")
    return "\n".join(lines)


def _coerce_sqlite_val(cell: Any, col_type: str) -> Any:
    if cell is None:
        return None
    s = str(cell)
    stripped = s.strip()
    if stripped == "":
        return None
    if col_type == "INTEGER":
        try:
            return int(stripped)
        except Exception:
            return s
    if col_type == "REAL":
        try:
            val = float(stripped)
            return val if math.isfinite(val) else s
        except Exception:
            return s
    return s


def query_csv_in_memory(
    raw_text: str,
    ext: str,
    sql_text: str,
    *,
    limit: int = 50,
) -> Dict[str, Any]:
    """在内存隔离只读 SQLite 沙箱（表名固定 `csv_data`）中执行 SQL 查询."""
    validated_sql = validate_readonly_sql(sql_text, db_type="SQLITE")
    delimiter, headers, data_rows, profile = _parse_and_profile_csv(raw_text, ext)
    _ = delimiter
    col_types = [c["inferred_type"] for c in (profile.get("columns") or [])]
    if len(col_types) != len(headers):
        col_types = ["TEXT"] * len(headers)

    quoted_cols_ddl = ", ".join(
        f'"{h.replace(chr(34), chr(34) * 2)}" {col_types[idx]}'
        for idx, h in enumerate(headers)
    )
    placeholders = ", ".join("?" for _ in headers)
    insert_sql = f'INSERT INTO "csv_data" VALUES ({placeholders})'
    coerced_rows = [
        tuple(_coerce_sqlite_val(row[idx], col_types[idx]) for idx in range(len(headers)))
        for row in data_rows
    ]

    eff_limit = max(1, min(200, int(limit or 50)))
    t0 = time.perf_counter()

    with contextlib.closing(sqlite3.connect(":memory:")) as mem_conn:
        mem_conn.execute(f'CREATE TABLE "csv_data" ({quoted_cols_ddl})')
        if coerced_rows:
            mem_conn.executemany(insert_sql, coerced_rows)
        mem_conn.commit()
        mem_conn.execute("PRAGMA query_only = ON")

        start_mono = time.monotonic()
        vm_steps = 0

        def _guard_progress() -> int:
            nonlocal vm_steps
            vm_steps += 1
            if vm_steps > 2000 or (time.monotonic() - start_mono) > 3.0:
                return 1
            return 0

        mem_conn.set_progress_handler(_guard_progress, 10000)
        try:
            cur = mem_conn.execute(validated_sql)
            desc = cur.description or []
            fetched = cur.fetchmany(eff_limit + 1)
        finally:
            mem_conn.set_progress_handler(None, 0)

    truncated = len(fetched) > eff_limit
    rows_out = [list(r) for r in fetched[:eff_limit]]
    columns_out = [{"name": str(d[0]), "data_type": "ANY"} for d in desc]
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return {
        "ok": True,
        "sql": validated_sql,
        "table_name": "csv_data",
        "columns": columns_out,
        "rows": rows_out,
        "row_count": len(rows_out),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
    }
