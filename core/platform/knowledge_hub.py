"""Data.O.G 平台级零依赖离线轻量化知识库中枢 (KnowledgeHub).

核心设计：
1. 严格零新增第三方依赖（增量体积 0 KB）：
   - 原生标准库解析 `.md` / `.txt` / `.sql` / `.html` / `.htm` / `.json` / `.csv` / `.docx`（ZIP+XML 防 XXE 与解压膨胀）
   - 复用已有 `openpyxl` 解析 `.xlsx`
   - 借助 SQLite 内置 FTS5 + BM25 实现毫秒级离线全文本检索（若运行期无 FTS5 自动退回纯 Python BM25）
2. 混合词法切词 (`tokenize_kb_text`)：
   - 完整保留 `ORA-00942`、`ERROR 1064`、`SQLSTATE`、`DISTRIBUTE_BY` 等报错码与 SQL 标识符
   - 中文字段拆分为单字 + 重叠二元组，精准命中中文数据库运维概念
3. 严守 Layer 2 本地私有保险箱隔离 (`scope="private"`)，绝不撑大团队 `.dataog.db` 工作区，
   同时支持按方言导出/导入轻量 `.json.gz` 知识包供团队离线分享。
4. 支持在后台配置可选的 OpenAI 兼容 `/embeddings` 接口进行语义混排（RRF 倒数秩融合）；
   处于纯离线环境或接口不可用时自动无感退回纯 FTS5，绝不阻塞使用。
"""

from __future__ import annotations

from array import array
import csv
import datetime
import gzip
import hashlib
from html.parser import HTMLParser
import io
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
import uuid
import xml.etree.ElementTree as ET
import zipfile

import openpyxl

from core.platform.builtin_dialect_kb import (
    BUILTIN_DIALECT_DOCS,
    BUILTIN_KB_VERSION,
    DIALECT_LABEL_MAP,
    SUPPORTED_KB_DIALECTS,
    normalize_kb_dialect,
)
from core.platform.import_progress import ProgressCallback, noop_progress
from core.platform.llm_hub import LLMHub, is_masked_secret_input, validate_llm_base_url
from core.platform.storage import PlatformStateStore

ALLOWED_KB_EXTENSIONS: Tuple[str, ...] = (
    ".md",
    ".txt",
    ".sql",
    ".html",
    ".htm",
    ".json",
    ".csv",
    ".docx",
    ".xlsx",
)

MAX_KB_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
MAX_XLSX_TEXT_BYTES = 2 * 1024 * 1024
MAX_PACK_DECOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_PACK_DOCS = 500
EMBEDDING_CONFIG_META_KEY = "kb_embedding_config"

_ERROR_CODE_RE = re.compile(r"\b(?:[A-Za-z]{2,8}-\d{3,6}|SQLSTATE\s*[A-Za-z0-9]{4,8}|ERROR\s+\d{3,6})\b", re.IGNORECASE)
_ASCII_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$.]{1,64}|\d+(?:\.\d+)?")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_SQL_COMMENT_SECTION_RE = re.compile(r"^--\s*(?:={2,}|-{2,}|\[.*\]|第.+[章节]|【.+】)\s*(.*)$")


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _decode_text_bytes(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def tokenize_kb_text(text: str) -> str:
    """生成兼顾中文、SQL 标识符与报错码的混合检索词元串."""
    src = str(text or "")
    if not src:
        return ""
    out: List[str] = []

    # 1. 保留完整报错码（含连字符形式与归一化无空格形式）
    for m in _ERROR_CODE_RE.finditer(src):
        tok = re.sub(r"\s+", "_", m.group(0).strip().lower())
        out.append(tok)
        compact = tok.replace("-", "").replace("_", "")
        if compact and compact != tok:
            out.append(compact)

    # 2. 英文单词、SQL 关键字与标识符（如 schema.table -> 同时保留整串与拆段）
    for m in _ASCII_IDENT_RE.finditer(src):
        raw_tok = m.group(0).strip(".").lower()
        if not raw_tok:
            continue
        out.append(raw_tok)
        if "_" in raw_tok or "." in raw_tok or "$" in raw_tok:
            for part in re.split(r"[._$]+", raw_tok):
                if len(part) >= 2:
                    out.append(part)

    # 3. 中文连续片段拆分为单字与双字重叠词元，保证 FTS5 unicode61 高召回
    for m in _CJK_RUN_RE.finditer(src):
        run = m.group(0)
        for ch in run:
            out.append(ch)
        for i in range(len(run) - 1):
            out.append(run[i : i + 2])

    return " ".join(out)


def _extract_query_terms(query: str) -> List[str]:
    tokens = tokenize_kb_text(query).split()
    seen: set[str] = set()
    unique: List[str] = []
    # 优先保留长度 >= 2 的高区分度词元，单字兜底
    ordered = sorted(tokens, key=lambda t: (0 if len(t) >= 2 else 1))
    for t in ordered:
        if t not in seen:
            seen.add(t)
            unique.append(t)
        if len(unique) >= 28:
            break
    return unique


class _SafeHtmlSectionParser(HTMLParser):
    """标准库 HTML 解析器：忽略 script/style，提取结构化标题、代码块与表格文本."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignore_depth = 0
        self._in_heading = False
        self._in_pre = False
        self._heading_buf: List[str] = []
        self._body_buf: List[str] = []
        self._headings: List[str] = []
        self.sections: List[Tuple[str, str]] = []
        self._current_heading = "正文"

    def _flush_section(self) -> None:
        text = re.sub(r"\n{3,}", "\n\n", "".join(self._body_buf)).strip()
        if text:
            self.sections.append((self._current_heading, text))
        self._body_buf.clear()

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "svg"):
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return
        if t in ("h1", "h2", "h3", "h4"):
            self._flush_section()
            self._in_heading = True
            self._heading_buf.clear()
        elif t == "pre":
            self._in_pre = True
            self._body_buf.append("\n```\n")
        elif t in ("p", "div", "li", "tr", "br", "blockquote", "section", "article"):
            self._body_buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript", "svg"):
            self._ignore_depth = max(0, self._ignore_depth - 1)
            return
        if self._ignore_depth > 0:
            return
        if t in ("h1", "h2", "h3", "h4") and self._in_heading:
            self._in_heading = False
            title = re.sub(r"\s+", " ", "".join(self._heading_buf)).strip()
            self._heading_buf.clear()
            if title:
                level = int(t[1])
                while len(self._headings) >= level:
                    self._headings.pop()
                self._headings.append(title)
                self._current_heading = " / ".join(self._headings)
        elif t == "pre" and self._in_pre:
            self._in_pre = False
            self._body_buf.append("\n```\n")
        elif t in ("td", "th"):
            self._body_buf.append(" | ")
        elif t in ("p", "tr", "li"):
            self._body_buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return
        if self._in_heading:
            self._heading_buf.append(data)
        else:
            self._body_buf.append(data)

    def finalize(self) -> List[Tuple[str, str]]:
        self._flush_section()
        return self.sections


def _parse_markdown_sections(text: str) -> List[Tuple[str, str]]:
    sections: List[Tuple[str, str]] = []
    heading_stack: List[Tuple[int, str]] = []
    current_heading = "总览"
    buf: List[str] = []
    in_fence = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence:
            m = _MD_HEADING_RE.match(stripped)
            if m:
                body = "\n".join(buf).strip()
                if body:
                    sections.append((current_heading, body))
                buf.clear()
                lvl = len(m.group(1))
                htext = m.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= lvl:
                    heading_stack.pop()
                heading_stack.append((lvl, htext))
                current_heading = " / ".join(h[1] for h in heading_stack)
                continue
        buf.append(line)

    tail = "\n".join(buf).strip()
    if tail:
        sections.append((current_heading, tail))
    return sections


def _parse_plain_or_sql_sections(text: str, *, is_sql: bool = False) -> List[Tuple[str, str]]:
    lines = text.splitlines()
    sections: List[Tuple[str, str]] = []
    current_heading = "SQL 脚本参考" if is_sql else "参考正文"
    buf: List[str] = []

    for line in lines:
        s = line.strip()
        if is_sql and s.startswith("--"):
            m = _SQL_COMMENT_SECTION_RE.match(s)
            if m:
                prev = "\n".join(buf).strip()
                if prev:
                    sections.append((current_heading, prev))
                    buf.clear()
                cand = (m.group(1) or s.lstrip("-= ")).strip()
                if cand:
                    current_heading = cand[:80]
                continue
        buf.append(line)

    tail = "\n".join(buf).strip()
    if tail:
        sections.append((current_heading, tail))
    return sections


def _parse_json_sections(text: str) -> List[Tuple[str, str]]:
    data = json.loads(text)
    sections: List[Tuple[str, str]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            pretty = json.dumps(v, ensure_ascii=False, indent=2)
            sections.append((str(k)[:100] or "JSON 节点", pretty))
    elif isinstance(data, list):
        batch_size = 20
        for idx in range(0, len(data), batch_size):
            chunk_items = data[idx : idx + batch_size]
            pretty = json.dumps(chunk_items, ensure_ascii=False, indent=2)
            sections.append((f"条目 {idx + 1}-{idx + len(chunk_items)}", pretty))
    else:
        sections.append(("JSON 内容", str(data)))
    return sections


def _parse_csv_sections(text: str) -> List[Tuple[str, str]]:
    reader = list(csv.reader(io.StringIO(text)))
    if not reader:
        return []
    header = reader[0]
    rows = reader[1:] if len(reader) > 1 else []
    if not rows:
        return [("CSV 表头", " | ".join(header))]
    sections: List[Tuple[str, str]] = []
    batch = 25
    header_line = " | ".join(header)
    sep_line = " | ".join("---" for _ in header)
    for idx in range(0, len(rows), batch):
        sub = rows[idx : idx + batch]
        lines = [header_line, sep_line]
        for r in sub:
            lines.append(" | ".join(str(c) for c in r))
        sections.append((f"CSV 行 {idx + 1}-{idx + len(sub)}", "\n".join(lines)))
    return sections


def _parse_docx_sections(raw_bytes: bytes) -> List[Tuple[str, str]]:
    if not raw_bytes.startswith(b"PK"):
        raise ValueError("DOCX 文件格式无效，缺少 ZIP 文件头")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("无法打开 DOCX 压缩包，文件可能已损坏") from exc

    with zf:
        if "word/document.xml" not in zf.namelist():
            raise ValueError("DOCX 文件结构异常，缺少 word/document.xml")
        info = zf.getinfo("word/document.xml")
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise ValueError("DOCX 正文 XML 体积超过安全限制 (20 MB)")
        with zf.open(info) as zstream:
            xml_bytes = zstream.read(MAX_DOCX_XML_BYTES + 1)
        if len(xml_bytes) > MAX_DOCX_XML_BYTES:
            raise ValueError("DOCX 正文 XML 解压超出安全限制 (20 MB)")

    xml_head = xml_bytes[:4096].upper()
    if b"<!DOCTYPE" in xml_head or b"<!ENTITY" in xml_head:
        raise ValueError("DOCX 包含禁止的 XML DOCTYPE/ENTITY 声明")

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError("DOCX XML 内容解析失败") from exc

    w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    body = root.find(f"{w_ns}body")
    if body is None:
        return []

    sections: List[Tuple[str, str]] = []
    current_heading = "文档正文"
    buf: List[str] = []

    def _flush() -> None:
        txt = "\n".join(buf).strip()
        if txt:
            sections.append((current_heading, txt))
        buf.clear()

    for child in body:
        tag = child.tag
        if tag == f"{w_ns}p":
            texts = [t.text for t in child.iter(f"{w_ns}t") if t.text]
            p_text = "".join(texts).strip()
            if not p_text:
                continue
            p_style = child.find(f"{w_ns}pPr/{w_ns}pStyle")
            style_val = str(p_style.attrib.get(f"{w_ns}val", "") if p_style is not None else "")
            if style_val.lower().startswith(("heading", "title", "标题")):
                _flush()
                current_heading = p_text[:100]
            else:
                buf.append(p_text)
        elif tag == f"{w_ns}tbl":
            row_lines: List[str] = []
            for tr in child.findall(f"{w_ns}tr"):
                cells: List[str] = []
                for tc in tr.findall(f"{w_ns}tc"):
                    c_txt = "".join(t.text for t in tc.iter(f"{w_ns}t") if t.text).strip()
                    cells.append(re.sub(r"\s+", " ", c_txt))
                if any(cells):
                    row_lines.append(" | ".join(cells))
            if row_lines:
                buf.append("\n".join(row_lines))

    _flush()
    return sections


def _parse_xlsx_sections(raw_bytes: bytes) -> List[Tuple[str, str]]:
    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    sections: List[Tuple[str, str]] = []
    total_chars = 0
    try:
        for sheet in wb.worksheets:
            title = str(sheet.title or "Sheet").strip()
            header: Optional[List[str]] = None
            batch_lines: List[str] = []
            row_idx = 0
            for raw_row in sheet.iter_rows(values_only=True):
                row_vals = ["" if v is None else str(v).strip() for v in raw_row]
                if not any(row_vals):
                    continue
                row_idx += 1
                if row_idx > 5000:
                    batch_lines.append("... (超出单 Sheet 5000 行索引上限已截断)")
                    break
                if header is None:
                    header = row_vals
                    batch_lines.append(" | ".join(header))
                    batch_lines.append(" | ".join("---" for _ in header))
                    continue
                line = " | ".join(row_vals)
                batch_lines.append(line)
                total_chars += len(line)
                if len(batch_lines) >= 35:
                    sections.append((title, "\n".join(batch_lines)))
                    batch_lines = [" | ".join(header), " | ".join("---" for _ in header)]
                if total_chars > MAX_XLSX_TEXT_BYTES:
                    break
            if batch_lines and (len(batch_lines) > 2 or row_idx == 1):
                sections.append((title, "\n".join(batch_lines)))
            if total_chars > MAX_XLSX_TEXT_BYTES:
                break
    finally:
        wb.close()
    return sections


def parse_document_to_sections(filename: str, raw_bytes: bytes) -> List[Tuple[str, str]]:
    """根据扩展名用零新增依赖解析器提取 [(heading_path, text)] 章节列表."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_KB_EXTENSIONS:
        raise ValueError(
            f"不支持的知识库文件格式 `{ext or '未知'}`，仅支持: {', '.join(ALLOWED_KB_EXTENSIONS)}"
        )
    if not raw_bytes:
        raise ValueError("上传的参考资料文件内容为空")
    if len(raw_bytes) > MAX_KB_UPLOAD_BYTES:
        raise ValueError(f"单份参考资料大小不能超过 {MAX_KB_UPLOAD_BYTES // (1024 * 1024)} MB")

    if ext == ".docx":
        sections = _parse_docx_sections(raw_bytes)
    elif ext == ".xlsx":
        sections = _parse_xlsx_sections(raw_bytes)
    else:
        text = _decode_text_bytes(raw_bytes)
        if ext == ".md":
            sections = _parse_markdown_sections(text)
        elif ext in (".html", ".htm"):
            parser = _SafeHtmlSectionParser()
            parser.feed(text)
            parser.close()
            sections = parser.finalize()
        elif ext == ".json":
            sections = _parse_json_sections(text)
        elif ext == ".csv":
            sections = _parse_csv_sections(text)
        else:
            sections = _parse_plain_or_sql_sections(text, is_sql=(ext == ".sql"))

    cleaned = [(h.strip() or "正文", body.strip()) for h, body in sections if body and body.strip()]
    if not cleaned:
        raise ValueError("未能从文件中提取出有效文本内容")
    return cleaned


def chunk_sections(
    sections: Sequence[Tuple[str, str]],
    *,
    target_chars: int = 750,
    max_chars: int = 1200,
    overlap_chars: int = 100,
) -> List[Tuple[str, str]]:
    """按章节边界与字符预算切分为结构完整、附带微重叠的检索片段."""
    chunks: List[Tuple[str, str]] = []

    for heading, body in sections:
        clean_body = re.sub(r"\n{3,}", "\n\n", body.strip())
        if len(clean_body) <= max_chars:
            chunks.append((heading, clean_body))
            continue

        paragraphs = [p.strip() for p in re.split(r"\n{2,}", clean_body) if p.strip()]
        buf: List[str] = []
        buf_len = 0

        for para in paragraphs:
            if len(para) > max_chars:
                if buf:
                    chunks.append((heading, "\n\n".join(buf)))
                    buf.clear()
                    buf_len = 0
                start = 0
                step = max(200, target_chars - overlap_chars)
                while start < len(para):
                    piece = para[start : start + max_chars].strip()
                    if piece:
                        chunks.append((heading, piece))
                    if start + max_chars >= len(para):
                        break
                    start += step
                continue

            if buf_len + len(para) + 2 > target_chars and buf:
                joined = "\n\n".join(buf)
                chunks.append((heading, joined))
                tail = joined[-overlap_chars:].strip() if overlap_chars > 0 and len(joined) > overlap_chars else ""
                buf = [tail] if tail else []
                buf_len = len(tail)
            buf.append(para)
            buf_len += len(para) + 2

        if buf:
            chunks.append((heading, "\n\n".join(buf)))

    return chunks


def _pack_vector(vec: Sequence[float]) -> bytes:
    arr = array("f", [float(x) for x in vec])
    return arr.tobytes()


def _unpack_vector(blob: bytes) -> List[float]:
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return (dot / denom) if denom > 1e-12 else 0.0


class KnowledgeHub:
    """Data.O.G 平台共享轻量级离线知识库服务."""

    def __init__(self, store: PlatformStateStore, llm_hub: LLMHub) -> None:
        self.store = store
        self.llm_hub = llm_hub
        self._fts5_available = False
        self._init_schema()
        self._seed_builtin_docs()

    def _init_schema(self) -> None:
        self.store.register_table_schema(
            "kb_documents",
            """
            CREATE TABLE IF NOT EXISTS kb_documents (
                doc_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                dialect TEXT NOT NULL DEFAULT 'GENERIC',
                source_type TEXT NOT NULL DEFAULT 'upload',
                file_name TEXT NOT NULL DEFAULT '',
                file_ext TEXT NOT NULL DEFAULT '.md',
                file_size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                summary TEXT NOT NULL DEFAULT '',
                chunk_count INTEGER NOT NULL DEFAULT 0,
                is_builtin INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_kb_docs_dialect
                ON kb_documents (dialect, is_builtin, enabled);
            """,
            [
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("dialect", "TEXT NOT NULL DEFAULT 'GENERIC'"),
                ("source_type", "TEXT NOT NULL DEFAULT 'upload'"),
                ("file_name", "TEXT NOT NULL DEFAULT ''"),
                ("file_ext", "TEXT NOT NULL DEFAULT '.md'"),
                ("file_size", "INTEGER NOT NULL DEFAULT 0"),
                ("sha256", "TEXT NOT NULL DEFAULT ''"),
                ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("summary", "TEXT NOT NULL DEFAULT ''"),
                ("chunk_count", "INTEGER NOT NULL DEFAULT 0"),
                ("is_builtin", "INTEGER NOT NULL DEFAULT 0"),
                ("enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="private",
        )
        self.store.register_table_schema(
            "kb_chunks",
            """
            CREATE TABLE IF NOT EXISTS kb_chunks (
                chunk_id TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL DEFAULT '',
                dialect TEXT NOT NULL DEFAULT 'GENERIC',
                chunk_index INTEGER NOT NULL DEFAULT 0,
                heading_path TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                char_count INTEGER NOT NULL DEFAULT 0,
                tokens_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc
                ON kb_chunks (doc_id, chunk_index ASC);
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_dialect
                ON kb_chunks (dialect);
            """,
            [
                ("doc_id", "TEXT NOT NULL DEFAULT ''"),
                ("dialect", "TEXT NOT NULL DEFAULT 'GENERIC'"),
                ("chunk_index", "INTEGER NOT NULL DEFAULT 0"),
                ("heading_path", "TEXT NOT NULL DEFAULT ''"),
                ("content", "TEXT NOT NULL DEFAULT ''"),
                ("char_count", "INTEGER NOT NULL DEFAULT 0"),
                ("tokens_text", "TEXT NOT NULL DEFAULT ''"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="private",
        )
        self.store.register_table_schema(
            "kb_embeddings",
            """
            CREATE TABLE IF NOT EXISTS kb_embeddings (
                chunk_id TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL DEFAULT '',
                dialect TEXT NOT NULL DEFAULT 'GENERIC',
                model TEXT NOT NULL DEFAULT '',
                dims INTEGER NOT NULL DEFAULT 0,
                vector_blob BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb_emb_doc
                ON kb_embeddings (doc_id, model);
            """,
            [
                ("doc_id", "TEXT NOT NULL DEFAULT ''"),
                ("dialect", "TEXT NOT NULL DEFAULT 'GENERIC'"),
                ("model", "TEXT NOT NULL DEFAULT ''"),
                ("dims", "INTEGER NOT NULL DEFAULT 0"),
                ("vector_blob", "BLOB NOT NULL"),
            ],
            scope="private",
        )
        self.store.register_private_tables(["kb_chunks_fts"])
        try:
            with self.store.module_db(scope="private") as conn:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
                        chunk_id UNINDEXED,
                        doc_id UNINDEXED,
                        dialect UNINDEXED,
                        heading_path,
                        tokens_text,
                        tokenize='unicode61 remove_diacritics 0'
                    );
                    """
                )
            self._fts5_available = True
        except Exception:
            self._fts5_available = False

    def _seed_builtin_docs(self) -> None:
        now = _now_str()
        with self.store.module_db(scope="private") as conn:
            existing = {
                str(r["doc_id"]): str(r["sha256"])
                for r in conn.execute(
                    "SELECT doc_id, sha256 FROM kb_documents WHERE is_builtin = 1"
                ).fetchall()
            }
            for item in BUILTIN_DIALECT_DOCS:
                doc_id = item["doc_id"]
                raw_text = item["content"]
                digest = hashlib.sha256(
                    f"{BUILTIN_KB_VERSION}\n{item['title']}\n{raw_text}".encode("utf-8")
                ).hexdigest()
                if existing.get(doc_id) == digest:
                    continue
                sections = _parse_markdown_sections(raw_text)
                chunks = chunk_sections(sections)
                self._upsert_doc_and_chunks_on_conn(
                    conn,
                    doc_id=doc_id,
                    title=item["title"],
                    dialect=item["dialect"],
                    source_type="builtin",
                    file_name=f"{doc_id}.md",
                    file_ext=".md",
                    file_size=len(raw_text.encode("utf-8")),
                    sha256=digest,
                    tags=[item["dialect"], "内置速查卡"],
                    summary=item["summary"],
                    chunks=chunks,
                    is_builtin=1,
                    enabled=1,
                    now=now,
                )

    def _upsert_doc_and_chunks_on_conn(
        self,
        conn: Any,
        *,
        doc_id: str,
        title: str,
        dialect: str,
        source_type: str,
        file_name: str,
        file_ext: str,
        file_size: int,
        sha256: str,
        tags: List[str],
        summary: str,
        chunks: Sequence[Tuple[str, str]],
        is_builtin: int,
        enabled: int,
        now: str,
    ) -> None:
        # 清理旧切片、FTS5 与向量
        conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM kb_embeddings WHERE doc_id = ?", (doc_id,))
        if self._fts5_available:
            conn.execute("DELETE FROM kb_chunks_fts WHERE doc_id = ?", (doc_id,))

        conn.execute(
            """
            INSERT INTO kb_documents (
                doc_id, title, dialect, source_type, file_name, file_ext,
                file_size, sha256, tags_json, summary, chunk_count,
                is_builtin, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                title = excluded.title,
                dialect = excluded.dialect,
                source_type = excluded.source_type,
                file_name = excluded.file_name,
                file_ext = excluded.file_ext,
                file_size = excluded.file_size,
                sha256 = excluded.sha256,
                tags_json = excluded.tags_json,
                summary = excluded.summary,
                chunk_count = excluded.chunk_count,
                is_builtin = excluded.is_builtin,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                doc_id,
                title,
                dialect,
                source_type,
                file_name,
                file_ext,
                int(file_size),
                sha256,
                json.dumps(tags, ensure_ascii=False),
                summary,
                len(chunks),
                int(is_builtin),
                int(enabled),
                now,
                now,
            ),
        )

        for idx, (heading, body) in enumerate(chunks):
            cid = f"{doc_id}::c{idx:04d}"
            tok_text = tokenize_kb_text(f"{title} {heading} {body}")
            heading_tok = tokenize_kb_text(heading)
            conn.execute(
                """
                INSERT INTO kb_chunks (
                    chunk_id, doc_id, dialect, chunk_index, heading_path,
                    content, char_count, tokens_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    doc_id,
                    dialect,
                    idx,
                    heading,
                    body,
                    len(body),
                    tok_text,
                    now,
                ),
            )
            if self._fts5_available:
                conn.execute(
                    """
                    INSERT INTO kb_chunks_fts (
                        chunk_id, doc_id, dialect, heading_path, tokens_text
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (cid, doc_id, dialect, heading_tok, tok_text),
                )

    @staticmethod
    def _row_to_doc_dict(row: Any) -> Dict[str, Any]:
        try:
            tags = json.loads(row["tags_json"] or "[]")
            if not isinstance(tags, list):
                tags = []
        except Exception:
            tags = []
        dialect = str(row["dialect"] or "GENERIC")
        return {
            "doc_id": str(row["doc_id"]),
            "title": str(row["title"] or ""),
            "dialect": dialect,
            "dialect_label": DIALECT_LABEL_MAP.get(dialect, dialect),
            "source_type": str(row["source_type"] or "upload"),
            "file_name": str(row["file_name"] or ""),
            "file_ext": str(row["file_ext"] or ".md"),
            "file_size": int(row["file_size"] or 0),
            "sha256": str(row["sha256"] or ""),
            "tags": [str(t) for t in tags if t],
            "summary": str(row["summary"] or ""),
            "chunk_count": int(row["chunk_count"] or 0),
            "is_builtin": bool(row["is_builtin"]),
            "enabled": bool(row["enabled"]),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def list_supported_dialects(self) -> List[Dict[str, str]]:
        return [{"code": code, "label": label} for code, label in SUPPORTED_KB_DIALECTS]

    def get_summary(self) -> Dict[str, Any]:
        with self.store.module_db(scope="private") as conn:
            doc_rows = conn.execute(
                "SELECT dialect, is_builtin, enabled, chunk_count, file_size FROM kb_documents"
            ).fetchall()
            emb_cnt = conn.execute("SELECT COUNT(*) FROM kb_embeddings").fetchone()[0]

        user_docs = 0
        builtin_docs = 0
        enabled_docs = 0
        total_chunks = 0
        total_bytes = 0
        by_dialect: Dict[str, int] = {}
        for r in doc_rows:
            if r["is_builtin"]:
                builtin_docs += 1
            else:
                user_docs += 1
            if r["enabled"]:
                enabled_docs += 1
            total_chunks += int(r["chunk_count"] or 0)
            total_bytes += int(r["file_size"] or 0)
            dl = str(r["dialect"] or "GENERIC")
            by_dialect[dl] = by_dialect.get(dl, 0) + 1

        emb_cfg = self.get_embedding_config()
        return {
            "total_docs": user_docs + builtin_docs,
            "user_docs": user_docs,
            "builtin_docs": builtin_docs,
            "enabled_docs": enabled_docs,
            "total_chunks": total_chunks,
            "total_bytes": total_bytes,
            "embedded_chunks": int(emb_cnt or 0),
            "fts5_available": self._fts5_available,
            "embedding_enabled": bool(emb_cfg.get("enabled")),
            "by_dialect": by_dialect,
            "supported_extensions": list(ALLOWED_KB_EXTENSIONS),
            "supported_dialects": self.list_supported_dialects(),
        }

    def list_documents(
        self,
        *,
        dialect: str = "",
        include_builtin: bool = True,
        keyword: str = "",
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if dialect and dialect.strip().upper() not in ("ALL", "*"):
            clauses.append("dialect = ?")
            params.append(normalize_kb_dialect(dialect))
        if not include_builtin:
            clauses.append("is_builtin = 0")
        kw = (keyword or "").strip().lower()
        if kw:
            clauses.append("(LOWER(title) LIKE ? OR LOWER(file_name) LIKE ? OR LOWER(summary) LIKE ?)")
            like = f"%{kw}%"
            params.extend([like, like, like])
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM kb_documents {where_sql} ORDER BY is_builtin ASC, updated_at DESC, doc_id ASC"
        with self.store.module_db(scope="private") as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_doc_dict(r) for r in rows]

    def get_document(self, doc_id: str, *, include_chunks: bool = True) -> Optional[Dict[str, Any]]:
        with self.store.module_db(scope="private") as conn:
            row = conn.execute("SELECT * FROM kb_documents WHERE doc_id = ?", (doc_id,)).fetchone()
            if row is None:
                return None
            doc = self._row_to_doc_dict(row)
            if include_chunks:
                crows = conn.execute(
                    "SELECT chunk_id, chunk_index, heading_path, content, char_count "
                    "FROM kb_chunks WHERE doc_id = ? ORDER BY chunk_index ASC",
                    (doc_id,),
                ).fetchall()
                doc["chunks"] = [dict(c) for c in crows]
            return doc

    def ingest_uploaded_file(
        self,
        *,
        filename: str,
        content: bytes,
        dialect: str = "GENERIC",
        title: str = "",
        tags: Optional[List[str]] = None,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        safe_name = os.path.basename((filename or "").strip() or "reference.md")
        ext = os.path.splitext(safe_name)[1].lower()
        eff_dialect = normalize_kb_dialect(dialect)
        progress_cb(15, "解析文档", f"正在提取 {safe_name} 文本结构...", 0, 1)
        sections = parse_document_to_sections(safe_name, content)

        progress_cb(50, "智能切分", f"已识别 {len(sections)} 个章节，正在切分知识块...", 0, 1)
        chunks = chunk_sections(sections)
        if not chunks:
            raise ValueError("未能从文档切分出有效知识片段")

        sha = hashlib.sha256(content).hexdigest()
        eff_title = (title or "").strip() or os.path.splitext(safe_name)[0]
        eff_tags = [str(t).strip() for t in (tags or []) if str(t).strip()][:12]
        summary = re.sub(r"\s+", " ", chunks[0][1])[:160]
        doc_id = f"kb_{uuid.uuid4().hex[:12]}"
        now = _now_str()

        progress_cb(78, "建立离线索引", f"正在写入 {len(chunks)} 个片段至 FTS5 全文索引...", 0, 1)
        with self.store.module_db(scope="private") as conn:
            self._upsert_doc_and_chunks_on_conn(
                conn,
                doc_id=doc_id,
                title=eff_title,
                dialect=eff_dialect,
                source_type="upload",
                file_name=safe_name,
                file_ext=ext,
                file_size=len(content),
                sha256=sha,
                tags=eff_tags,
                summary=summary,
                chunks=chunks,
                is_builtin=0,
                enabled=1,
                now=now,
            )

        # 若可选开启了在线 Embedding，尝试增量向量化（失败不阻断上传）
        emb_warning = ""
        try:
            emb_cfg = self.get_embedding_config()
            if emb_cfg.get("enabled") and emb_cfg.get("model"):
                self._embed_doc_chunks_best_effort(doc_id)
        except Exception as exc:
            emb_warning = f"（已离线索引至 FTS5；向量增量生成跳过: {exc}）"

        doc = self.get_document(doc_id, include_chunks=False)
        progress_cb(100, "导入完成", f"已收录《{eff_title}》({len(chunks)} 段)", len(chunks), len(chunks))
        return {
            "ok": True,
            "document": doc,
            "message": f"已收录知识文档《{eff_title}》（{eff_dialect} · 共 {len(chunks)} 个切片）{emb_warning}",
        }

    def ingest_text_document(
        self,
        *,
        title: str,
        content: str,
        dialect: str = "GENERIC",
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        clean_title = (title or "").strip()
        clean_body = (content or "").strip()
        if not clean_title:
            raise ValueError("知识文档标题不能为空")
        if not clean_body:
            raise ValueError("知识文档内容不能为空")
        raw_bytes = clean_body.encode("utf-8")
        return self.ingest_uploaded_file(
            filename=f"{clean_title[:48]}.md",
            content=raw_bytes,
            dialect=dialect,
            title=clean_title,
            tags=tags,
        )

    def update_document(
        self,
        doc_id: str,
        *,
        title: Optional[str] = None,
        dialect: Optional[str] = None,
        tags: Optional[List[str]] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        with self.store.module_db(scope="private") as conn:
            row = conn.execute("SELECT * FROM kb_documents WHERE doc_id = ?", (doc_id,)).fetchone()
            if row is None:
                return None
            new_title = str(title).strip() if title is not None and str(title).strip() else str(row["title"])
            new_dialect = (
                normalize_kb_dialect(dialect)
                if dialect is not None and not row["is_builtin"]
                else str(row["dialect"])
            )
            new_tags = (
                json.dumps([str(t).strip() for t in tags if str(t).strip()][:12], ensure_ascii=False)
                if tags is not None
                else str(row["tags_json"])
            )
            new_enabled = int(bool(enabled)) if enabled is not None else int(row["enabled"])
            now = _now_str()
            conn.execute(
                """
                UPDATE kb_documents
                SET title = ?, dialect = ?, tags_json = ?, enabled = ?, updated_at = ?
                WHERE doc_id = ?
                """,
                (new_title, new_dialect, new_tags, new_enabled, now, doc_id),
            )
            if new_dialect != str(row["dialect"]):
                conn.execute("UPDATE kb_chunks SET dialect = ? WHERE doc_id = ?", (new_dialect, doc_id))
                conn.execute("UPDATE kb_embeddings SET dialect = ? WHERE doc_id = ?", (new_dialect, doc_id))
                if self._fts5_available:
                    conn.execute("UPDATE kb_chunks_fts SET dialect = ? WHERE doc_id = ?", (new_dialect, doc_id))
        return self.get_document(doc_id, include_chunks=False)

    def delete_document(self, doc_id: str) -> bool:
        with self.store.module_db(scope="private") as conn:
            row = conn.execute(
                "SELECT doc_id, is_builtin FROM kb_documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                return False
            if row["is_builtin"]:
                raise ValueError("内置方言速查卡不允许删除，可选择禁用")
            conn.execute("DELETE FROM kb_embeddings WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            if self._fts5_available:
                conn.execute("DELETE FROM kb_chunks_fts WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM kb_documents WHERE doc_id = ?", (doc_id,))
        return True

    def get_dialect_cheatsheet_brief(self, dialect: str, *, max_chars: int = 1800) -> str:
        eff_dialect = normalize_kb_dialect(dialect)
        target_ids = [f"builtin_{eff_dialect.lower()}"] if eff_dialect != "GENERIC" else []
        if "builtin_generic" not in target_ids:
            target_ids.append("builtin_generic")
        parts: List[str] = []
        with self.store.module_db(scope="private") as conn:
            for did in target_ids:
                drow = conn.execute(
                    "SELECT doc_id, title, enabled FROM kb_documents WHERE doc_id = ?",
                    (did,),
                ).fetchone()
                if drow is None or not drow["enabled"]:
                    continue
                crows = conn.execute(
                    "SELECT heading_path, content FROM kb_chunks WHERE doc_id = ? ORDER BY chunk_index ASC LIMIT 4",
                    (did,),
                ).fetchall()
                if crows:
                    sec_txt = "\n".join(f"### {c['heading_path']}\n{c['content']}" for c in crows)
                    parts.append(f"【{drow['title']}】\n{sec_txt}")
        merged = "\n\n".join(parts)
        return merged[:max_chars]

    def search(
        self,
        query: str,
        *,
        dialect: str = "",
        top_k: int = 6,
        include_builtin: bool = True,
        use_embeddings: bool = True,
    ) -> Dict[str, Any]:
        clean_q = (query or "").strip()
        safe_k = max(1, min(20, int(top_k or 6)))
        raw_dl = (dialect or "").strip().upper()
        filter_by_dialect = bool(raw_dl and raw_dl not in ("ALL", "*"))
        eff_dialect = normalize_kb_dialect(raw_dl) if filter_by_dialect else ""

        if not clean_q:
            return {
                "ok": True,
                "query": "",
                "dialect": eff_dialect or "ALL",
                "retrieval_mode": "empty",
                "hits": [],
            }

        terms = _extract_query_terms(clean_q)
        if not terms:
            return {
                "ok": True,
                "query": clean_q,
                "dialect": eff_dialect or "ALL",
                "retrieval_mode": "empty",
                "hits": [],
            }

        allowed_dialects: Tuple[str, ...] = ()
        if filter_by_dialect:
            if eff_dialect == "GENERIC":
                allowed_dialects = ("GENERIC",)
            else:
                allowed_dialects = (eff_dialect, "GENERIC")

        fts_ranked = self._search_fts_or_python_bm25(
            terms=terms,
            allowed_dialects=allowed_dialects,
            include_builtin=include_builtin,
            limit=max(safe_k * 3, 18),
        )

        retrieval_mode = "fts5" if self._fts5_available else "bm25_py"
        merged_ranked = list(fts_ranked)

        if use_embeddings:
            emb_cfg = self._load_embedding_config_raw()
            if emb_cfg.get("enabled") and emb_cfg.get("model"):
                try:
                    vec_ranked = self._search_vectors(
                        clean_q,
                        emb_cfg=emb_cfg,
                        allowed_dialects=allowed_dialects,
                        include_builtin=include_builtin,
                        limit=max(safe_k * 3, 18),
                    )
                    if vec_ranked:
                        merged_ranked = self._rrf_merge(fts_ranked, vec_ranked)
                        retrieval_mode = "hybrid_fts5_embedding"
                except Exception:
                    retrieval_mode = "fts5_offline_fallback"

        hits: List[Dict[str, Any]] = []
        for item in merged_ranked[:safe_k]:
            content = str(item["content"] or "")
            snippet = re.sub(r"\s+", " ", content).strip()
            if len(snippet) > 240:
                snippet = snippet[:237] + "..."
            hits.append(
                {
                    "chunk_id": item["chunk_id"],
                    "doc_id": item["doc_id"],
                    "doc_title": item["doc_title"],
                    "dialect": item["dialect"],
                    "is_builtin": bool(item["is_builtin"]),
                    "chunk_index": int(item["chunk_index"]),
                    "heading_path": item["heading_path"],
                    "snippet": snippet,
                    "content": content,
                    "score": round(float(item["score"]), 4),
                }
            )

        return {
            "ok": True,
            "query": clean_q,
            "dialect": eff_dialect or "ALL",
            "retrieval_mode": retrieval_mode,
            "hits": hits,
        }

    def _search_fts_or_python_bm25(
        self,
        *,
        terms: List[str],
        allowed_dialects: Tuple[str, ...],
        include_builtin: bool,
        limit: int,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if self._fts5_available:
            safe_terms = [f'"{t.replace(chr(34), "")}"' for t in terms if t.strip()]
            if safe_terms:
                match_expr = " OR ".join(safe_terms)
                clauses = ["d.enabled = 1", "kb_chunks_fts MATCH ?"]
                params: List[Any] = [match_expr]
                if allowed_dialects:
                    ph = ",".join("?" for _ in allowed_dialects)
                    clauses.append(f"c.dialect IN ({ph})")
                    params.extend(allowed_dialects)
                if not include_builtin:
                    clauses.append("d.is_builtin = 0")
                params.append(limit)

                sql = f"""
                    SELECT
                        c.chunk_id,
                        c.doc_id,
                        d.title AS doc_title,
                        c.dialect,
                        d.is_builtin,
                        c.chunk_index,
                        c.heading_path,
                        c.content,
                        bm25(kb_chunks_fts) AS raw_bm25
                    FROM kb_chunks_fts
                    JOIN kb_chunks c ON c.chunk_id = kb_chunks_fts.chunk_id
                    JOIN kb_documents d ON d.doc_id = c.doc_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY raw_bm25 ASC
                    LIMIT ?
                """
                with self.store.module_db(scope="private") as conn:
                    rows = conn.execute(sql, tuple(params)).fetchall()
                for r in rows:
                    # FTS5 bm25 返回负值（越小越相关）
                    base_score = max(0.01, -float(r["raw_bm25"] or 0.0))
                    # 用户上传官方文档优先级高于内置速查卡
                    boost = 1.35 if not r["is_builtin"] else 1.0
                    # 目标精准方言略高于 GENERIC
                    if allowed_dialects and len(allowed_dialects) > 1 and r["dialect"] == allowed_dialects[0]:
                        boost *= 1.15
                    results.append(
                        {
                            "chunk_id": str(r["chunk_id"]),
                            "doc_id": str(r["doc_id"]),
                            "doc_title": str(r["doc_title"]),
                            "dialect": str(r["dialect"]),
                            "is_builtin": int(r["is_builtin"]),
                            "chunk_index": int(r["chunk_index"]),
                            "heading_path": str(r["heading_path"]),
                            "content": str(r["content"]),
                            "score": base_score * boost,
                        }
                    )
                results.sort(key=lambda x: x["score"], reverse=True)
                return results

        # 纯 Python 词频兜底（无 FTS5 时自动启用）
        clauses = ["d.enabled = 1"]
        params2: List[Any] = []
        if allowed_dialects:
            ph = ",".join("?" for _ in allowed_dialects)
            clauses.append(f"c.dialect IN ({ph})")
            params2.extend(allowed_dialects)
        if not include_builtin:
            clauses.append("d.is_builtin = 0")
        with self.store.module_db(scope="private") as conn:
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.doc_id, d.title AS doc_title, c.dialect,
                       d.is_builtin, c.chunk_index, c.heading_path, c.content, c.tokens_text
                FROM kb_chunks c
                JOIN kb_documents d ON d.doc_id = c.doc_id
                WHERE {' AND '.join(clauses)}
                """,
                tuple(params2),
            ).fetchall()

        term_set = set(terms)
        for r in rows:
            toks = str(r["tokens_text"] or "").split()
            if not toks:
                continue
            hits = sum(1 for t in toks if t in term_set)
            if hits == 0:
                continue
            boost = 1.35 if not r["is_builtin"] else 1.0
            score = (hits / math.sqrt(len(toks) + 1.0)) * boost
            results.append(
                {
                    "chunk_id": str(r["chunk_id"]),
                    "doc_id": str(r["doc_id"]),
                    "doc_title": str(r["doc_title"]),
                    "dialect": str(r["dialect"]),
                    "is_builtin": int(r["is_builtin"]),
                    "chunk_index": int(r["chunk_index"]),
                    "heading_path": str(r["heading_path"]),
                    "content": str(r["content"]),
                    "score": score,
                }
            )
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def _search_vectors(
        self,
        query: str,
        *,
        emb_cfg: Dict[str, Any],
        allowed_dialects: Tuple[str, ...],
        include_builtin: bool,
        limit: int,
    ) -> List[Dict[str, Any]]:
        q_vecs = self.llm_hub.create_embeddings(
            [query],
            provider_id=str(emb_cfg.get("provider_id") or ""),
            model=str(emb_cfg.get("model") or ""),
            base_url=str(emb_cfg.get("base_url") or ""),
            api_key=str(emb_cfg.get("api_key") or ""),
            timeout_sec=4.0,
        )
        if not q_vecs:
            return []
        q_vec = q_vecs[0]

        clauses = ["d.enabled = 1", "e.model = ?"]
        params: List[Any] = [str(emb_cfg.get("model") or "")]
        if allowed_dialects:
            ph = ",".join("?" for _ in allowed_dialects)
            clauses.append(f"c.dialect IN ({ph})")
            params.extend(allowed_dialects)
        if not include_builtin:
            clauses.append("d.is_builtin = 0")

        with self.store.module_db(scope="private") as conn:
            rows = conn.execute(
                f"""
                SELECT e.vector_blob, c.chunk_id, c.doc_id, d.title AS doc_title,
                       c.dialect, d.is_builtin, c.chunk_index, c.heading_path, c.content
                FROM kb_embeddings e
                JOIN kb_chunks c ON c.chunk_id = e.chunk_id
                JOIN kb_documents d ON d.doc_id = c.doc_id
                WHERE {' AND '.join(clauses)}
                """,
                tuple(params),
            ).fetchall()

        scored: List[Dict[str, Any]] = []
        for r in rows:
            vec = _unpack_vector(r["vector_blob"])
            sim = _cosine_similarity(q_vec, vec)
            if sim <= 0.05:
                continue
            boost = 1.15 if not r["is_builtin"] else 1.0
            scored.append(
                {
                    "chunk_id": str(r["chunk_id"]),
                    "doc_id": str(r["doc_id"]),
                    "doc_title": str(r["doc_title"]),
                    "dialect": str(r["dialect"]),
                    "is_builtin": int(r["is_builtin"]),
                    "chunk_index": int(r["chunk_index"]),
                    "heading_path": str(r["heading_path"]),
                    "content": str(r["content"]),
                    "score": sim * boost,
                }
            )
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    @staticmethod
    def _rrf_merge(
        fts_items: List[Dict[str, Any]],
        vec_items: List[Dict[str, Any]],
        *,
        k: int = 60,
    ) -> List[Dict[str, Any]]:
        by_id: Dict[str, Dict[str, Any]] = {}
        scores: Dict[str, float] = {}
        for rank, item in enumerate(fts_items, start=1):
            cid = item["chunk_id"]
            by_id[cid] = item
            scores[cid] = scores.get(cid, 0.0) + (1.0 / (k + rank))
        for rank, item in enumerate(vec_items, start=1):
            cid = item["chunk_id"]
            if cid not in by_id:
                by_id[cid] = item
            scores[cid] = scores.get(cid, 0.0) + (1.0 / (k + rank))

        merged: List[Dict[str, Any]] = []
        for cid, item in by_id.items():
            copy_item = dict(item)
            copy_item["score"] = scores[cid] * (1.2 if not item["is_builtin"] else 1.0)
            merged.append(copy_item)
        merged.sort(key=lambda x: x["score"], reverse=True)
        return merged

    def read_section(self, chunk_id: str, *, window: int = 1) -> Optional[Dict[str, Any]]:
        win = max(0, min(4, int(window or 1)))
        with self.store.module_db(scope="private") as conn:
            target = conn.execute(
                "SELECT * FROM kb_chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
            if target is None:
                return None
            doc_row = conn.execute(
                "SELECT * FROM kb_documents WHERE doc_id = ?", (target["doc_id"],)
            ).fetchone()
            if doc_row is None:
                return None
            cidx = int(target["chunk_index"])
            neighbors = conn.execute(
                """
                SELECT chunk_id, chunk_index, heading_path, content, char_count
                FROM kb_chunks
                WHERE doc_id = ? AND chunk_index BETWEEN ? AND ?
                ORDER BY chunk_index ASC
                """,
                (target["doc_id"], max(0, cidx - win), cidx + win),
            ).fetchall()
            outline = conn.execute(
                "SELECT chunk_id, chunk_index, heading_path FROM kb_chunks WHERE doc_id = ? ORDER BY chunk_index ASC",
                (target["doc_id"],),
            ).fetchall()

        return {
            "ok": True,
            "document": self._row_to_doc_dict(doc_row),
            "target_chunk_id": str(target["chunk_id"]),
            "target_chunk_index": cidx,
            "sections": [dict(n) for n in neighbors],
            "outline": [dict(o) for o in outline],
        }

    # =========================================================================
    # 轻量知识包导出/导入 (.json.gz)
    # =========================================================================

    def export_pack_bytes(
        self,
        *,
        dialect: str = "",
        doc_ids: Optional[List[str]] = None,
    ) -> Tuple[bytes, str]:
        clauses = ["is_builtin = 0"]
        params: List[Any] = []
        eff_dl = ""
        if dialect and dialect.strip().upper() not in ("ALL", "*"):
            eff_dl = normalize_kb_dialect(dialect)
            clauses.append("dialect = ?")
            params.append(eff_dl)
        if doc_ids:
            clean_ids = [str(d).strip() for d in doc_ids if str(d).strip()]
            if clean_ids:
                ph = ",".join("?" for _ in clean_ids)
                clauses.append(f"doc_id IN ({ph})")
                params.extend(clean_ids)

        with self.store.module_db(scope="private") as conn:
            docs = conn.execute(
                f"SELECT * FROM kb_documents WHERE {' AND '.join(clauses)} ORDER BY created_at ASC",
                tuple(params),
            ).fetchall()
            if not docs:
                raise ValueError("当前筛选条件下没有可导出的用户知识文档（内置速查卡无需重复导出）")
            export_docs: List[Dict[str, Any]] = []
            for d in docs:
                crows = conn.execute(
                    "SELECT chunk_index, heading_path, content FROM kb_chunks WHERE doc_id = ? ORDER BY chunk_index ASC",
                    (d["doc_id"],),
                ).fetchall()
                doc_dict = self._row_to_doc_dict(d)
                doc_dict["chunks"] = [dict(c) for c in crows]
                export_docs.append(doc_dict)

        pack_obj = {
            "format": "dataog_kb_pack_v1",
            "exported_at": _now_str(),
            "dialect_filter": eff_dl or "ALL",
            "doc_count": len(export_docs),
            "documents": export_docs,
        }
        raw_json = json.dumps(pack_obj, ensure_ascii=False).encode("utf-8")
        gz_bytes = gzip.compress(raw_json, compresslevel=6)
        fname = f"DataOG_KB_{eff_dl or 'ALL'}_{datetime.datetime.now().strftime('%Y%m%d')}.json.gz"
        return gz_bytes, fname

    def import_pack_bytes(
        self,
        raw_bytes: bytes,
        *,
        overwrite_existing: bool = False,
    ) -> Dict[str, Any]:
        if not raw_bytes:
            raise ValueError("导入的知识包文件为空")
        if len(raw_bytes) > MAX_KB_UPLOAD_BYTES * 2:
            raise ValueError("知识包压缩文件体积超过安全上限 (30 MB)")

        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw_bytes), mode="rb") as gz:
                decompressed = gz.read(MAX_PACK_DECOMPRESSED_BYTES + 1)
        except Exception as exc:
            raise ValueError("无法解压 .json.gz 知识包，请检查文件格式") from exc

        if len(decompressed) > MAX_PACK_DECOMPRESSED_BYTES:
            raise ValueError("知识包解压后超过安全体积限制 (50 MB)")

        try:
            pack = json.loads(decompressed.decode("utf-8"))
        except Exception as exc:
            raise ValueError("知识包 JSON 解析失败") from exc

        if not isinstance(pack, dict) or pack.get("format") != "dataog_kb_pack_v1":
            raise ValueError("不支持的知识包格式（缺少 dataog_kb_pack_v1 标识）")
        docs = pack.get("documents")
        if not isinstance(docs, list) or not docs:
            raise ValueError("知识包中不包含有效文档条目")
        if len(docs) > MAX_PACK_DOCS:
            raise ValueError(f"单次导入知识包最多包含 {MAX_PACK_DOCS} 篇文档")

        imported_count = 0
        skipped_count = 0
        now = _now_str()

        with self.store.module_db(scope="private") as conn:
            existing_shas = {
                str(r["sha256"]): str(r["doc_id"])
                for r in conn.execute(
                    "SELECT doc_id, sha256 FROM kb_documents WHERE is_builtin = 0 AND sha256 != ''"
                ).fetchall()
            }
            for item in docs:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or item.get("file_name") or "").strip()
                raw_chunks = item.get("chunks") or []
                if not title or not isinstance(raw_chunks, list) or not raw_chunks:
                    continue
                chunks = [
                    (
                        str(c.get("heading_path") or "正文")[:120],
                        str(c.get("content") or "").strip(),
                    )
                    for c in raw_chunks
                    if isinstance(c, dict) and str(c.get("content") or "").strip()
                ]
                if not chunks:
                    continue
                eff_dialect = normalize_kb_dialect(str(item.get("dialect") or "GENERIC"))
                sha = str(item.get("sha256") or "").strip() or hashlib.sha256(
                    "\n".join(c[1] for c in chunks).encode("utf-8")
                ).hexdigest()

                dup_id = existing_shas.get(sha)
                if dup_id and not overwrite_existing:
                    skipped_count += 1
                    continue

                doc_id = dup_id if (dup_id and overwrite_existing) else f"kb_{uuid.uuid4().hex[:12]}"
                tags = [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()][:12]
                file_name = os.path.basename(str(item.get("file_name") or f"{title}.md"))
                file_ext = str(item.get("file_ext") or ".md")
                file_size = int(item.get("file_size") or sum(len(c[1].encode("utf-8")) for c in chunks))
                summary = str(item.get("summary") or re.sub(r"\s+", " ", chunks[0][1])[:160])

                self._upsert_doc_and_chunks_on_conn(
                    conn,
                    doc_id=doc_id,
                    title=title,
                    dialect=eff_dialect,
                    source_type="pack",
                    file_name=file_name,
                    file_ext=file_ext,
                    file_size=file_size,
                    sha256=sha,
                    tags=tags,
                    summary=summary,
                    chunks=chunks,
                    is_builtin=0,
                    enabled=1 if item.get("enabled", True) else 0,
                    now=now,
                )
                existing_shas[sha] = doc_id
                imported_count += 1

        return {
            "ok": True,
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "message": f"知识包导入完成：新增/更新 {imported_count} 篇，跳过重复 {skipped_count} 篇",
        }

    # =========================================================================
    # 可选 Embedding 配置与批量向量化
    # =========================================================================

    def _load_embedding_config_raw(self) -> Dict[str, Any]:
        raw = self.store.get_meta(EMBEDDING_CONFIG_META_KEY, {})
        if not isinstance(raw, dict):
            raw = {}
        return {
            "enabled": bool(raw.get("enabled", False)),
            "provider_id": str(raw.get("provider_id") or ""),
            "base_url": str(raw.get("base_url") or ""),
            "api_key": str(raw.get("api_key") or ""),
            "model": str(raw.get("model") or ""),
        }

    def get_embedding_config(self) -> Dict[str, Any]:
        cfg = self._load_embedding_config_raw()
        key = cfg["api_key"]
        masked = ""
        if key:
            masked = f"{key[:4]}••••{key[-4:]}" if len(key) > 8 else "******"
        return {
            "enabled": cfg["enabled"],
            "provider_id": cfg["provider_id"],
            "base_url": cfg["base_url"],
            "has_api_key": bool(key),
            "api_key_masked": masked,
            "model": cfg["model"],
        }

    def save_embedding_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        existing = self._load_embedding_config_raw()
        enabled = bool(payload.get("enabled", existing["enabled"]))
        provider_id = str(payload.get("provider_id") if "provider_id" in payload else existing["provider_id"]).strip().lower()
        raw_base_url = str(payload.get("base_url") if "base_url" in payload else existing["base_url"]).strip()
        base_url = validate_llm_base_url(raw_base_url) if raw_base_url else ""
        model = str(payload.get("model") if "model" in payload else existing["model"]).strip()

        clear_key = bool(payload.get("clear_api_key", False))
        incoming_key = payload.get("api_key")
        if clear_key:
            api_key = ""
        elif incoming_key is not None and not is_masked_secret_input(str(incoming_key)):
            api_key = str(incoming_key).strip()
        else:
            api_key = existing["api_key"]

        if enabled and not model:
            raise ValueError("启用 Embedding 语义混排时必须指定模型名称")

        self.store.save_meta(
            EMBEDDING_CONFIG_META_KEY,
            {
                "enabled": enabled,
                "provider_id": provider_id,
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
            },
        )
        return self.get_embedding_config()

    def _embed_doc_chunks_best_effort(self, doc_id: str) -> int:
        cfg = self._load_embedding_config_raw()
        if not cfg.get("enabled") or not cfg.get("model"):
            return 0
        with self.store.module_db(scope="private") as conn:
            rows = conn.execute(
                "SELECT chunk_id, dialect, heading_path, content FROM kb_chunks WHERE doc_id = ? ORDER BY chunk_index ASC",
                (doc_id,),
            ).fetchall()
        if not rows:
            return 0

        texts = [f"{r['heading_path']}\n{r['content']}"[:2500] for r in rows]
        inserted = 0
        batch_size = 16
        for i in range(0, len(rows), batch_size):
            batch_rows = rows[i : i + batch_size]
            batch_texts = texts[i : i + batch_size]
            vecs = self.llm_hub.create_embeddings(
                batch_texts,
                provider_id=cfg["provider_id"],
                model=cfg["model"],
                base_url=cfg["base_url"],
                api_key=cfg["api_key"],
                timeout_sec=15.0,
            )
            with self.store.module_db(scope="private") as conn:
                for r, v in zip(batch_rows, vecs):
                    conn.execute(
                        """
                        INSERT INTO kb_embeddings (chunk_id, doc_id, dialect, model, dims, vector_blob)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(chunk_id) DO UPDATE SET
                            model = excluded.model,
                            dims = excluded.dims,
                            vector_blob = excluded.vector_blob
                        """,
                        (
                            str(r["chunk_id"]),
                            doc_id,
                            str(r["dialect"]),
                            cfg["model"],
                            len(v),
                            _pack_vector(v),
                        ),
                    )
                    inserted += 1
        return inserted

    def reindex_embeddings(
        self,
        *,
        doc_id: str = "",
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        cfg = self._load_embedding_config_raw()
        if not cfg.get("enabled") or not cfg.get("model"):
            raise ValueError("请先开启并配置 Embedding 模型后再重建向量索引")

        with self.store.module_db(scope="private") as conn:
            if doc_id:
                docs = conn.execute(
                    "SELECT doc_id, title FROM kb_documents WHERE doc_id = ? AND enabled = 1",
                    (doc_id,),
                ).fetchall()
            else:
                docs = conn.execute(
                    "SELECT doc_id, title FROM kb_documents WHERE enabled = 1 ORDER BY is_builtin ASC"
                ).fetchall()

        total_docs = len(docs)
        embedded_total = 0
        for idx, d in enumerate(docs, start=1):
            pct = round((idx - 1) / max(1, total_docs) * 90.0 + 5.0, 1)
            progress_cb(pct, "生成向量索引", f"正在向量化《{d['title']}》...", idx - 1, total_docs)
            embedded_total += self._embed_doc_chunks_best_effort(str(d["doc_id"]))

        progress_cb(100, "向量重建完成", f"已为 {embedded_total} 个知识片段建立向量索引", total_docs, total_docs)
        return {
            "ok": True,
            "embedded_chunks": embedded_total,
            "doc_count": total_docs,
            "message": f"已完成 {total_docs} 篇文档共 {embedded_total} 个切片的向量索引",
        }
