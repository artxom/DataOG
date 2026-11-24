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

INLINE_TOKEN_BUDGET = 3500
CHUNK_TARGET_TOKENS = 2200
CHUNK_HARD_MAX_TOKENS = 2800
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


def get_attachment_limits() -> Dict[str, Any]:
    """返回前端展示与预检用的附件限制常量."""
    return {
        "max_csv_bytes": MAX_CSV_BYTES,
        "max_text_bytes": MAX_TEXT_BYTES,
        "inline_token_budget": INLINE_TOKEN_BUDGET,
        "chunk_target_tokens": CHUNK_TARGET_TOKENS,
        "chunk_hard_max_tokens": CHUNK_HARD_MAX_TOKENS,
        "default_batch_max_chunks": DEFAULT_BATCH_MAX_CHUNKS,
        "max_attachments_per_turn": MAX_ATTACHMENTS_PER_TURN,
        "allowed_extensions": sorted(ALLOWED_ATTACHMENT_EXTENSIONS),
        "csv_extensions": sorted(CSV_EXTENSIONS),
        "text_extensions": sorted(TEXT_EXTENSIONS),
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


def _chunk_sql_text(text: str) -> List[Dict[str, Any]]:
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


def recommend_strategy(file_kind: str, estimated_tokens: int) -> str:
    if estimated_tokens <= INLINE_TOKEN_BUDGET:
        return "inline"
    if file_kind == "csv":
        return "on_demand"
    return "stepped_batch"


def process_attachment_payload(filename: str, raw_bytes: bytes) -> Dict[str, Any]:
    """统一入口：校验、解码、计算 Token、生成 CSV 画像与语义分块清单."""
    safe_name, ext, text, enc, file_kind = validate_and_decode_attachment(filename, raw_bytes)
    lines_cnt = len(text.splitlines())
    est_tokens = estimate_tokens(text)

    row_count = 0
    csv_profile: Dict[str, Any] = {}
    if file_kind == "csv":
        delimiter, headers, data_rows, csv_profile = _parse_and_profile_csv(text, ext)
        row_count = len(data_rows)
        chunks = _chunk_csv_rows(delimiter, headers, data_rows)
        summary = (
            f"CSV 数据表：{row_count} 行 × {len(headers)} 列 | 估算 ~{est_tokens} tokens | 切分为 {len(chunks)} 片"
        )
    elif file_kind == "sql":
        chunks = _chunk_sql_text(text)
        summary = f"SQL 脚本：{lines_cnt} 行 | 估算 ~{est_tokens} tokens | 切分为 {len(chunks)} 个语义块"
    else:
        chunks = _chunk_plain_text(text, label_prefix="文本段落")
        summary = f"纯文本文档：{lines_cnt} 行 | 估算 ~{est_tokens} tokens | 切分为 {len(chunks)} 段"

    oversized = est_tokens > INLINE_TOKEN_BUDGET
    strategy = recommend_strategy(file_kind, est_tokens)
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
        "chunks": chunks,
    }


def format_attachment_manifest(att: Dict[str, Any]) -> str:
    """生成极致紧凑的文件总览地图（< 450 tokens），绝不把大文件正文全量塞进 Prompt."""
    lines = [
        f"附件 `{att.get('filename')}` [id={att.get('attachment_id')}, 类型={att.get('file_kind')}, "
        f"{att.get('line_count', 0)} 行, ~{att.get('estimated_tokens', 0)} tokens, 共 {att.get('chunk_count', 0)} 片]"
    ]
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
        lines.append("  分片目录（可用 `inspect_attachment_chunk` 按 chunk_index 读取正文）:")
        for ch in chunks[:18]:
            lines.append(
                f"    - chunk_index={ch['chunk_index']}: {ch['title']} [{ch['range_label']}] (~{ch['estimated_tokens']} tok)"
            )
        if len(chunks) > 18:
            lines.append(f"    - ... 共 {len(chunks)} 片，后续可通过 `search_attachment` 检索定位")
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
