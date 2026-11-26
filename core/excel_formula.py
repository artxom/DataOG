"""Excel 单元格错误值清洗（#N/A 等置空）与纯 Python 公式结果兜底求值引擎.

核心能力：
1. 统一将 Excel 常见公式错误值（`#N/A`、`#VALUE!`、`#REF!`、`#DIV/0!`、`#NUM!`、`#NAME?`、`#NULL!`、
   `#SPILL!`、`#CALC!`、`#GETTING_DATA` 等）识别为空字符串 `""`；
2. 优先读取 `.xlsx` 中由 Excel/WPS 缓存的公式最终计算结果（`data_only=True`，含显式空串 `<c t="str"><v/></c>`）；
3. 当工作簿中存在未缓存 `<v>` 的公式单元格（如程序生成、未触发重算保存、拖拽共享公式或文本态公式）时，
   采用只读流式值加载 + 稀疏 XML 公式提取与纯 Python 求值器计算最终值（支持跨 Sheet 引用、整列引用 `A:F`、
   名称管理器 `<definedNames>`、超级表结构化引用 `Table1[列]`/`[@列]`、多条件数组匹配 `(A:A=x)*(B:B=y)`、
   `VLOOKUP`、`HLOOKUP`、`XLOOKUP`、`INDEX`+`MATCH`、`IF`、`IFS`、`IFERROR`、`IFNA`、
   `CONCAT`/`CONCATENATE`/`&`、`TRIM`、`UPPER`、`LOWER`、`LEFT`/`RIGHT`/`MID`、`SUBSTITUTE` 等）。
"""

from __future__ import annotations

import io
import math
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

# OOXML / Excel / WPS 全量标准错误标记（统一转小写比对）
EXCEL_ERROR_TOKENS: frozenset[str] = frozenset(
    {
        "#n/a",
        "#n/a!",
        "#na",
        "#value!",
        "#ref!",
        "#div/0!",
        "#num!",
        "#name?",
        "#null!",
        "#getting_data",
        "#spill!",
        "#calc!",
        "#field!",
        "#connect!",
        "#blocked!",
        "#unknown!",
        "#error!",
    }
)

# 编码列 / 物理类型列 / 长度精度列常见的空占位符
PLACEHOLDER_EMPTY_TOKENS: frozenset[str] = frozenset(
    {
        "/",
        "-",
        "--",
        "—",
        "——",
        "none",
        "null",
        "nil",
        "n/a",
        "na",
    }
)

_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")

# 识别被误存为纯文本的公式字符串（排除类似 "=1:有效" 的普通说明文本）
_TEXT_FORMULA_RE = re.compile(
    r"^=\s*(?:_xlfn\.|_xlws\.)?(?:"
    r"[A-Za-z_][A-Za-z0-9_.]*\s*\("
    r"|(?:'(?:[^']|'')+'|[A-Za-z0-9_\u4e00-\u9fff.]+)!\$?[A-Za-z]{1,3}\$?\d+"
    r"|\$?[A-Za-z]{1,3}\$?\d+\s*(?:$|&|[+\-*/^=<>])"
    r"|\[\s*@"
    r")"
)

_CELL_COORD_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")
_SHIFT_COORD_RE = re.compile(r"^(\$?)([A-Za-z]{1,3})(\$?)(\d+)$")
_SHIFT_COL_RE = re.compile(r"^(\$?)([A-Za-z]{1,3})$")

_MAX_ARRAY_CELLS = 120_000


def is_excel_error_value(val: Any) -> bool:
    """判断任意单元格值是否为 Excel 错误值（如 #N/A、#REF!、#VALUE! 等）."""
    if val is None or isinstance(val, (bool, int, float)):
        return False
    if getattr(val, "__class__", None) is not None and val.__class__.__module__.startswith(
        "openpyxl.worksheet.formula"
    ):
        return False
    s = _ZERO_WIDTH_RE.sub("", str(val)).strip()
    if not s:
        return False
    if "＃" in s or "#" in s:
        norm = unicodedata.normalize("NFKC", s).strip()
        unquoted = norm.lstrip("'\"").rstrip("'\"").strip().lower()
        if unquoted in EXCEL_ERROR_TOKENS:
            return True
    return s.lower() in EXCEL_ERROR_TOKENS


def clean_excel_cell_str(val: Any, *, treat_placeholder_empty: bool = False) -> str:
    """将 Excel 单元格值安全清洗为字符串：整数浮点去 `.0`，`#N/A` 等错误值一律转为空串 `""`."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return ""
        if val.is_integer():
            return str(int(val))
        return str(val)
    if getattr(val, "__class__", None) is not None and val.__class__.__module__.startswith(
        "openpyxl.worksheet.formula"
    ):
        return ""

    s = _ZERO_WIDTH_RE.sub("", str(val)).replace("\u00a0", " ").strip()
    if not s:
        return ""
    if is_excel_error_value(s):
        return ""
    if treat_placeholder_empty:
        norm_p = unicodedata.normalize("NFKC", s).strip().lower()
        if norm_p in PLACEHOLDER_EMPTY_TOKENS or norm_p in EXCEL_ERROR_TOKENS:
            return ""
    return s


def _parse_coord(coord_str: str) -> Optional[Tuple[int, int]]:
    m = _CELL_COORD_RE.match((coord_str or "").strip())
    if not m:
        return None
    col_idx = column_index_from_string(m.group(1).upper())
    row_idx = int(m.group(2))
    return (row_idx, col_idx)


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _resolve_zip_rel_target(
    base_xml_path: str,
    raw_target: str,
    zip_names: Optional[Set[str]] = None,
) -> str:
    tgt = (raw_target or "").strip().replace("\\", "/")
    if not tgt:
        return ""
    candidates: List[str] = []
    if tgt.startswith("/"):
        norm_abs = posixpath.normpath(tgt.lstrip("/"))
        if norm_abs not in ("", "."):
            candidates.append(norm_abs)
    else:
        base_dir = posixpath.dirname(base_xml_path)
        resolved = posixpath.normpath(posixpath.join(base_dir, tgt))
        norm_tgt = posixpath.normpath(tgt)
        if tgt.lower().startswith("xl/"):
            # 兼容第三方工具写入无前导 '/' 的 Target="xl/worksheets/..." 或 Target="xl/tables/..."，
            # 优先采用包根相对路径，避免拼接成 xl/xl/... 或 xl/worksheets/xl/...
            candidates.append(norm_tgt)
            candidates.append(resolved)
        else:
            if not resolved.lower().startswith("xl/") and base_xml_path.lower().startswith("xl/"):
                candidates.append(posixpath.normpath(posixpath.join("xl", tgt)))
            candidates.append(resolved)
            candidates.append(norm_tgt)

    if zip_names:
        for cand in candidates:
            if cand in zip_names:
                return cand
        lower_map = {n.lower(): n for n in zip_names}
        for cand in candidates:
            hit = lower_map.get(cand.lower())
            if hit is not None:
                return hit

    return candidates[0] if candidates else ""


def _map_sheet_xml_and_workbook_meta(
    zf: zipfile.ZipFile,
) -> Tuple[
    Dict[str, str],
    List[str],
    Dict[Tuple[Optional[str], str], str],
    Dict[str, Dict[str, Any]],
]:
    """解析工作表映射、名称管理器 `<definedNames>` 与 Excel 超级表 `xl/tables/*.xml`."""
    mapping: Dict[str, str] = {}
    ordered_titles: List[str] = []
    defined_names: Dict[Tuple[Optional[str], str], str] = {}
    tables_meta: Dict[str, Dict[str, Any]] = {}

    try:
        names = set(zf.namelist())
        if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
            return (mapping, ordered_titles, defined_names, tables_meta)

        rid_to_target: Dict[str, str] = {}
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels_root:
            if _local_tag(rel.tag) == "Relationship":
                rid = rel.attrib.get("Id", "")
                tgt = _resolve_zip_rel_target(
                    "xl/workbook.xml", rel.attrib.get("Target", ""), names
                )
                if rid and tgt:
                    rid_to_target[rid] = tgt

        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        for elem in wb_root.iter():
            lt = _local_tag(elem.tag)
            if lt == "sheet":
                sh_name = elem.attrib.get("name", "")
                rid = ""
                for ak, av in elem.attrib.items():
                    if ak == "id" or ak.endswith("}id"):
                        rid = av
                        break
                if sh_name:
                    ordered_titles.append(sh_name)
                xml_path = rid_to_target.get(rid, "")
                if xml_path and sh_name:
                    mapping[xml_path] = sh_name

        for elem in wb_root.iter():
            if _local_tag(elem.tag) == "definedName":
                if elem.attrib.get("hidden") in ("1", "true"):
                    continue
                dn_name = (elem.attrib.get("name") or "").strip()
                dn_text = (elem.text or "").strip()
                if not dn_name or not dn_text or dn_name.startswith("_xlnm."):
                    continue
                if dn_text.startswith("="):
                    dn_text = dn_text[1:].strip()
                if not dn_text:
                    continue
                local_id = elem.attrib.get("localSheetId")
                scope_sheet: Optional[str] = None
                if local_id is not None and local_id.isdigit():
                    idx_0b = int(local_id)
                    if 0 <= idx_0b < len(ordered_titles):
                        scope_sheet = ordered_titles[idx_0b].lower()
                defined_names[(scope_sheet, dn_name.lower())] = dn_text

        # 解析超级表 (Structured Tables) 归属工作表与列定义
        for xml_path, sh_title in mapping.items():
            sheet_dir = posixpath.dirname(xml_path)
            sheet_fn = posixpath.basename(xml_path)
            rels_path = f"{sheet_dir}/_rels/{sheet_fn}.rels"
            if rels_path not in names:
                continue
            s_rels = ET.fromstring(zf.read(rels_path))
            for rel in s_rels:
                if _local_tag(rel.tag) != "Relationship":
                    continue
                r_type = rel.attrib.get("Type", "")
                if not r_type.endswith("/table"):
                    continue
                tbl_path = _resolve_zip_rel_target(
                    xml_path, rel.attrib.get("Target", ""), names
                )
                if not tbl_path or tbl_path not in names:
                    continue
                t_root = ET.fromstring(zf.read(tbl_path))
                ref = t_root.attrib.get("ref", "")
                if ":" in ref:
                    p1, p2 = ref.split(":", 1)
                    rc1, rc2 = _parse_coord(p1), _parse_coord(p2)
                else:
                    rc1 = _parse_coord(ref)
                    rc2 = rc1
                if not rc1 or not rc2:
                    continue
                r1, c1 = min(rc1[0], rc2[0]), min(rc1[1], rc2[1])
                r2, c2 = max(rc1[0], rc2[0]), max(rc1[1], rc2[1])
                hdr_cnt = int(t_root.attrib.get("headerRowCount", "1") or 0)
                tot_cnt = int(t_root.attrib.get("totalsRowCount", "0") or 0)
                col_names: List[str] = []
                for ch in t_root.iter():
                    if _local_tag(ch.tag) == "tableColumn":
                        col_names.append((ch.attrib.get("name") or "").strip())
                col_map = {
                    cn.lower(): c1 + offset
                    for offset, cn in enumerate(col_names)
                    if cn
                }
                tbl_info = {
                    "sheet": sh_title,
                    "r1": r1,
                    "c1": c1,
                    "r2": r2,
                    "c2": c2,
                    "header_rows": hdr_cnt,
                    "totals_rows": tot_cnt,
                    "columns": col_map,
                }
                for key_attr in ("displayName", "name"):
                    t_name = (t_root.attrib.get(key_attr) or "").strip()
                    if t_name:
                        tables_meta[t_name.lower()] = tbl_info
    except Exception:
        pass
    return (mapping, ordered_titles, defined_names, tables_meta)


def _map_sheet_xml_to_titles(zf: zipfile.ZipFile) -> Dict[str, str]:
    mapping, _, _, _ = _map_sheet_xml_and_workbook_meta(zf)
    return mapping


# =========================================================================
# 纯 Python 轻量级 Excel 公式词法分析、语法树与求值器
# =========================================================================

_TOKEN_MASTER_RE = re.compile(
    r"""
    (?P<SPACE>\s+)
    |(?P<STR>"(?:""|[^"])*")
    |(?P<ERR>\#(?:N/A!?|NA|VALUE!|REF!|DIV/0!|NUM!|NAME\?|NULL!|GETTING_DATA|SPILL!|CALC!|ERROR!))
    |(?P<QSHEET>'(?:''|[^'])+'!)
    |(?P<USHEET>(?:\[[^\]]+\])?[A-Za-z0-9_\u4e00-\u9fff.]+!)
    |(?P<ROWREF>\$\d+)
    |(?P<NUM>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
    |(?P<OP><=|>=|<>|[=<>+\-*/^&%@(),;:{}])
    |(?P<IDENT>\$?[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_.\u4e00-\u9fff$]*|\$[A-Za-z]{1,3})
    """,
    re.VERBOSE | re.IGNORECASE,
)

_COL_ONLY_RE = re.compile(r"^\$?([A-Za-z]{1,3})$")
_ROW_R_ATTR_RE = re.compile(rb'<(?:\w+:)?row\b[^>]*\br=["\'](\d+)["\']')


def _scan_balanced_brackets(expr: str, start_pos: int) -> Tuple[str, int]:
    """扫描 Excel 结构化引用中的平衡方括号（如 `[英文]` 或 `[[#This Row],[字段名]]`）."""
    depth = 0
    i = start_pos
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "'":
            # Excel 结构化引用中单引号转义特殊字符（如 ']）
            i += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return (expr[start_pos : i + 1], i + 1)
        i += 1
    raise ValueError("未闭合的结构化引用方括号")


def _shift_shared_formula(master_expr: str, dr: int, dc: int) -> str:
    """按共享公式偏移量 (dr, dc) 平移非绝对锁定的相对单元格、整列与整行坐标（完整保护结构化表引用）."""
    if dr == 0 and dc == 0:
        return master_expr
    parts: List[str] = []
    pos = 0
    n = len(master_expr)

    while pos < n:
        if master_expr[pos] == "[":
            try:
                b_spec, b_end = _scan_balanced_brackets(master_expr, pos)
                parts.append(b_spec)
                pos = b_end
                continue
            except ValueError:
                parts.append(master_expr[pos])
                pos += 1
                continue

        m = _TOKEN_MASTER_RE.match(master_expr, pos)
        if not m:
            parts.append(master_expr[pos])
            pos += 1
            continue

        kind = m.lastgroup or ""
        val = m.group()
        start_pos = m.start()
        pos = m.end()

        if kind == "IDENT" and pos < n and master_expr[pos] == "[":
            try:
                b_spec, b_end = _scan_balanced_brackets(master_expr, pos)
                parts.append(f"{val}{b_spec}")
                pos = b_end
                continue
            except ValueError:
                parts.append(val)
                continue

        if kind == "IDENT":
            after = master_expr[pos:].lstrip()
            if after.startswith("(") or after.startswith("["):
                parts.append(val)
                continue
            mc = _SHIFT_COORD_RE.match(val)
            if mc:
                c_abs, col_s, r_abs, row_s = mc.groups()
                col_num = column_index_from_string(col_s.upper())
                row_num = int(row_s)
                if not c_abs:
                    col_num = max(1, col_num + dc)
                if not r_abs:
                    row_num = max(1, row_num + dr)
                parts.append(f"{c_abs}{get_column_letter(col_num)}{r_abs}{row_num}")
                continue
            prev_non_space = master_expr[:start_pos].rstrip()
            if after.startswith(":") or prev_non_space.endswith(":"):
                mcol = _SHIFT_COL_RE.match(val)
                if mcol:
                    c_abs, col_s = mcol.groups()
                    col_num = column_index_from_string(col_s.upper())
                    if not c_abs:
                        col_num = max(1, col_num + dc)
                    parts.append(f"{c_abs}{get_column_letter(col_num)}")
                    continue

        elif kind == "NUM" and val.isdigit() and dr != 0:
            after = master_expr[pos:].lstrip()
            prev_non_space = master_expr[:start_pos].rstrip()
            if after.startswith(":") or prev_non_space.endswith(":"):
                parts.append(str(max(1, int(val) + dr)))
                continue

        parts.append(val)

    return "".join(parts)


def _estimate_xml_sheet_max_row(raw: bytes) -> int:
    """从工作表 XML 尾部毫秒级提取真实最大行号（兼容 EasyExcel / POI SXSSF 写死 <dimension ref='A1:A1'/> 及 write_only 无 r 属性）."""
    if not raw:
        return 0
    tail = raw[-65536:] if len(raw) > 65536 else raw
    last_r = 0
    for m in _ROW_R_ATTR_RE.finditer(tail):
        last_r = max(last_r, int(m.group(1)))
    if last_r > 0:
        return last_r
    if len(raw) > 65536:
        for m in _ROW_R_ATTR_RE.finditer(raw):
            last_r = max(last_r, int(m.group(1)))
        if last_r > 0:
            return last_r
    return (
        raw.count(b"<row>")
        + raw.count(b"<row ")
        + raw.count(b":row>")
        + raw.count(b":row ")
    )


class _WorkbookPackageScan:
    """单次 ZIP 扫描结果：记录是否含未缓存公式、显式空串坐标、稀疏公式表、真实行数、命名区域与超级表."""

    __slots__ = (
        "has_uncached",
        "cached_empty_by_title",
        "formulas_by_title",
        "sheet_row_counts",
        "defined_names",
        "tables_meta",
        "normalized_content",
    )

    def __init__(
        self,
        *,
        has_uncached: bool = False,
        cached_empty_by_title: Optional[Dict[str, Set[Tuple[int, int]]]] = None,
        formulas_by_title: Optional[Dict[str, Dict[Tuple[int, int], str]]] = None,
        sheet_row_counts: Optional[Dict[str, int]] = None,
        defined_names: Optional[Dict[Tuple[Optional[str], str], str]] = None,
        tables_meta: Optional[Dict[str, Dict[str, Any]]] = None,
        normalized_content: Optional[bytes] = None,
    ) -> None:
        self.has_uncached = has_uncached
        self.cached_empty_by_title = cached_empty_by_title or {}
        self.formulas_by_title = formulas_by_title or {}
        self.sheet_row_counts = sheet_row_counts or {}
        self.defined_names = defined_names or {}
        self.tables_meta = tables_meta or {}
        self.normalized_content = normalized_content


def _repair_nonstandard_rels_if_needed(zf: zipfile.ZipFile, content: bytes) -> bytes:
    """当第三方工具生成的 .rels 含无前导 '/' 的包根路径（如 Target='xl/worksheets/...'）或反斜杠时，
    按需在内存中修正该 .rels 节点为 '/xl/...'，避免 openpyxl 拼接出 xl/xl/... 导致工作表丢失。
    常规合规 .xlsx 零拷贝直接原样返回 content。
    """
    names = set(zf.namelist())
    patched_rels: Dict[str, bytes] = {}
    for entry_name in names:
        if not entry_name.lower().endswith(".rels"):
            continue
        rels_dir = posixpath.dirname(entry_name)
        owner_dir = (
            posixpath.dirname(rels_dir)
            if posixpath.basename(rels_dir).lower() == "_rels"
            else rels_dir
        )
        base_xml = f"{owner_dir}/_owner.xml" if owner_dir else "_owner.xml"
        try:
            r_root = ET.fromstring(zf.read(entry_name))
        except Exception:
            continue
        changed = False
        for rel in r_root:
            if _local_tag(rel.tag) != "Relationship":
                continue
            if (rel.attrib.get("TargetMode") or "").strip().lower() == "external":
                continue
            raw_t = (rel.attrib.get("Target") or "").strip()
            if not raw_t:
                continue
            openpyxl_cand = (
                posixpath.normpath(raw_t.lstrip("/"))
                if raw_t.startswith("/")
                else posixpath.normpath(posixpath.join(owner_dir, raw_t))
            )
            if "\\" in raw_t or openpyxl_cand not in names:
                resolved = _resolve_zip_rel_target(base_xml, raw_t, names)
                if resolved and resolved in names:
                    rel.set("Target", f"/{resolved}")
                    changed = True
        if changed:
            ET.register_namespace("", "http://schemas.openxmlformats.org/package/2006/relationships")
            patched_rels[entry_name] = ET.tostring(r_root, encoding="utf-8", xml_declaration=True)

    if not patched_rels:
        return content

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w") as zout:
        for zinfo in zf.infolist():
            data = patched_rels.get(zinfo.filename)
            if data is None:
                data = zf.read(zinfo.filename)
            zout.writestr(zinfo, data)
    return out_buf.getvalue()


def _scan_xlsx_package(content: bytes) -> _WorkbookPackageScan:
    has_uncached = False
    cached_empty_by_title: Dict[str, Set[Tuple[int, int]]] = {}
    formulas_by_title: Dict[str, Dict[Tuple[int, int], str]] = {}
    sheet_row_counts: Dict[str, int] = {}

    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            xml_to_title, ordered_titles, defined_names, tables_meta = (
                _map_sheet_xml_and_workbook_meta(zf)
            )
            for name in zf.namelist():
                lower = name.lower()
                if not (
                    (lower.startswith("xl/worksheets/") and lower.endswith(".xml"))
                    or name in xml_to_title
                ):
                    continue
                sh_title = xml_to_title.get(name, "")
                if not sh_title:
                    m_idx = re.search(r"sheet(\d+)\.xml$", lower)
                    if m_idx:
                        i1 = int(m_idx.group(1))
                        if 1 <= i1 <= len(ordered_titles):
                            sh_title = ordered_titles[i1 - 1]
                    if not sh_title:
                        sh_title = name

                raw = zf.read(name)
                est_rows = _estimate_xml_sheet_max_row(raw)
                if est_rows > 0:
                    sheet_row_counts[sh_title] = est_rows

                if (
                    b"<f>" not in raw
                    and b"<f " not in raw
                    and b":f>" not in raw
                    and b":f " not in raw
                ):
                    continue

                empty_set: Set[Tuple[int, int]] = set()
                sheet_fmls: Dict[Tuple[int, int], str] = {}
                shared_masters: Dict[str, Tuple[int, int, str]] = {}
                pending_shared_children: List[Tuple[int, int, str]] = []

                seq_row = 0
                seq_col = 0

                for ev, elem in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
                    elem_tag = _local_tag(elem.tag)
                    if ev == "start":
                        if elem_tag == "row":
                            r_attr = elem.attrib.get("r", "")
                            seq_row = int(r_attr) if r_attr.isdigit() else (seq_row + 1)
                            seq_col = 0
                        continue

                    if elem_tag == "row":
                        elem.clear()
                        continue
                    if elem_tag != "c":
                        continue

                    seq_col += 1
                    rc = _parse_coord(elem.attrib.get("r", ""))
                    if rc is not None:
                        cur_r, cur_c = rc
                        seq_col = cur_c
                    else:
                        cur_r, cur_c = max(1, seq_row), seq_col

                    has_f = False
                    f_type = ""
                    f_si = ""
                    f_text: Optional[str] = None
                    has_v = False
                    has_is = False
                    v_text: Optional[str] = None
                    is_text_parts: List[str] = []

                    for ch in elem:
                        lt = _local_tag(ch.tag)
                        if lt == "f":
                            has_f = True
                            f_type = ch.attrib.get("t", "")
                            f_si = ch.attrib.get("si", "")
                            f_text = ch.text
                        elif lt == "v":
                            has_v = True
                            v_text = ch.text
                        elif lt == "is":
                            has_is = True
                            for sub in ch.iter():
                                if _local_tag(sub.tag) == "t" and sub.text:
                                    is_text_parts.append(sub.text)

                    if has_f:
                        c_type = elem.attrib.get("t", "")
                        # 提取稀疏公式表达式（支持普通公式、CSE 数组公式及拖拽共享公式）
                        cleaned_f = (f_text or "").strip()
                        if cleaned_f.startswith("{") and cleaned_f.endswith("}"):
                            cleaned_f = cleaned_f[1:-1].strip()
                        if cleaned_f.startswith("="):
                            cleaned_f = cleaned_f[1:].strip()

                        if f_type == "shared":
                            if cleaned_f:
                                if f_si != "":
                                    shared_masters[f_si] = (cur_r, cur_c, cleaned_f)
                                sheet_fmls[(cur_r, cur_c)] = cleaned_f
                            elif f_si != "":
                                if f_si in shared_masters:
                                    mr, mc_col, m_expr = shared_masters[f_si]
                                    sheet_fmls[(cur_r, cur_c)] = _shift_shared_formula(
                                        m_expr, cur_r - mr, cur_c - mc_col
                                    )
                                else:
                                    pending_shared_children.append((cur_r, cur_c, f_si))
                        elif cleaned_f:
                            sheet_fmls[(cur_r, cur_c)] = cleaned_f

                        # 严格判定是否已有 Excel/WPS 缓存的最终值：
                        # - <c t="str"><f>...</f><v/></c> 是显式缓存的空字符串 ""（并非未缓存！）
                        # - <c t="inlineStr"><f>...</f><is/></c> 同样是已缓存字符串
                        if has_is:
                            if not "".join(is_text_parts):
                                empty_set.add((cur_r, cur_c))
                        elif has_v and c_type == "str":
                            if v_text is None or v_text == "":
                                empty_set.add((cur_r, cur_c))
                        elif has_v and v_text is not None and v_text != "":
                            pass
                        else:
                            # 缺少 <v>/<is>（如程序生成尚未经 Excel 计算保存）才需要纯 Python 求值
                            if f_type != "dataTable":
                                has_uncached = True

                    elem.clear()

                for cr, cc, f_si in pending_shared_children:
                    if f_si in shared_masters:
                        mr, mc_col, m_expr = shared_masters[f_si]
                        sheet_fmls[(cr, cc)] = _shift_shared_formula(
                            m_expr, cr - mr, cc - mc_col
                        )

                if empty_set:
                    cached_empty_by_title[sh_title] = empty_set
                if sheet_fmls:
                    formulas_by_title[sh_title] = sheet_fmls

            norm_bytes = _repair_nonstandard_rels_if_needed(zf, content)
            return _WorkbookPackageScan(
                has_uncached=has_uncached,
                cached_empty_by_title=cached_empty_by_title,
                formulas_by_title=formulas_by_title,
                sheet_row_counts=sheet_row_counts,
                defined_names=defined_names,
                tables_meta=tables_meta,
                normalized_content=norm_bytes,
            )
    except Exception:
        return _WorkbookPackageScan(normalized_content=content)


def inspect_xlsx_formula_state(content: bytes) -> Tuple[bool, Dict[str, Set[Tuple[int, int]]]]:
    """快速扫描 .xlsx 工作表 XML，检测是否存在缺少缓存结果 `<v>` 的公式单元格.

    返回:
        (has_uncached_formula, cached_empty_by_sheet_title)
        其中显式空字符串 `<c t="str"><f>...</f><v/></c>` 被正确视为已缓存空串（`has_uncached_formula=False`）。
    """
    scan = _scan_xlsx_package(content)
    return (scan.has_uncached, scan.cached_empty_by_title)


class _RangeValue:
    """表示工作表矩形区域引用（支持懒求值与整列引用如 A:F）."""

    __slots__ = ("sheet", "r1", "c1", "r2", "c2", "whole_col")

    def __init__(
        self,
        sheet: str,
        r1: int,
        c1: int,
        r2: int,
        c2: int,
        whole_col: bool = False,
    ) -> None:
        self.sheet = sheet
        self.r1 = min(r1, r2)
        self.c1 = min(c1, c2)
        self.r2 = max(r1, r2)
        self.c2 = max(c1, c2)
        self.whole_col = whole_col


class _ArrayValue:
    """表示公式数组运算中间结果（如多条件 `(B2:B100=x)*(C2:C100=y)`）."""

    __slots__ = ("rows", "origin_r", "origin_c")

    def __init__(
        self,
        rows: List[List[Any]],
        origin_r: int = 1,
        origin_c: int = 1,
    ) -> None:
        self.rows = rows
        self.origin_r = origin_r
        self.origin_c = origin_c

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def width(self) -> int:
        return len(self.rows[0]) if self.rows else 0


def _tokenize_formula(expr: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    pos = 0
    n = len(expr)
    while pos < n:
        m = _TOKEN_MASTER_RE.match(expr, pos)
        if m:
            kind = m.lastgroup or ""
            val = m.group()
            end_pos = m.end()
            if kind == "SPACE":
                pos = end_pos
                continue
            if kind == "IDENT" and end_pos < n and expr[end_pos] == "[":
                # TableName[Column] 结构化表引用
                b_spec, b_end = _scan_balanced_brackets(expr, end_pos)
                tokens.append(("SREF", f"{val}{b_spec}"))
                pos = b_end
                continue
            tokens.append((kind, val))
            pos = end_pos
            continue

        if expr[pos] == "[":
            b_spec, b_end = _scan_balanced_brackets(expr, pos)
            tokens.append(("SREF", b_spec))
            pos = b_end
            continue

        raise ValueError(f"无法解析公式字符: {expr[pos : pos + 12]!r}")
    return tokens


def _unquote_sheet_token(tok_type: str, tok_val: str) -> str:
    raw = tok_val[:-1]  # 去除末尾 '!'
    if tok_type == "QSHEET" and raw.startswith("'") and raw.endswith("'"):
        raw = raw[1:-1].replace("''", "'")
    # 外部工作簿引用（如 [1]Sheet1! 或 '[外部标准.xlsx]Sheet1'!）：Excel 工作表名严禁含 [ ]，
    # 绝不能剥离 [1] 后误查当前工作簿的同名 Sheet1，统一返回不可达标记以触发 #REF!
    if "[" in raw and "]" in raw:
        return "__EXTERNAL_WB__"
    return raw


class _FormulaParser:
    """递归下降解析 Excel 公式为轻量元组 AST."""

    def __init__(self, tokens: List[Tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def _peek(self, offset: int = 0) -> Optional[Tuple[str, str]]:
        idx = self.pos + offset
        return self.tokens[idx] if 0 <= idx < len(self.tokens) else None

    def _eat(self) -> Tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self) -> Any:
        if not self.tokens:
            return ("lit", "")
        node = self._parse_comparison()
        if self.pos < len(self.tokens):
            raise ValueError("公式存在多余尾部标记")
        return node

    def _parse_comparison(self) -> Any:
        left = self._parse_concat()
        while True:
            t = self._peek()
            if t and t[0] == "OP" and t[1] in ("=", "<>", "<", "<=", ">", ">="):
                op = self._eat()[1]
                right = self._parse_concat()
                left = ("binop", op, left, right)
            else:
                break
        return left

    def _parse_concat(self) -> Any:
        left = self._parse_add_sub()
        while True:
            t = self._peek()
            if t and t[0] == "OP" and t[1] == "&":
                self._eat()
                right = self._parse_add_sub()
                left = ("binop", "&", left, right)
            else:
                break
        return left

    def _parse_add_sub(self) -> Any:
        left = self._parse_mul_div()
        while True:
            t = self._peek()
            if t and t[0] == "OP" and t[1] in ("+", "-"):
                op = self._eat()[1]
                right = self._parse_mul_div()
                left = ("binop", op, left, right)
            else:
                break
        return left

    def _parse_mul_div(self) -> Any:
        left = self._parse_power()
        while True:
            t = self._peek()
            if t and t[0] == "OP" and t[1] in ("*", "/"):
                op = self._eat()[1]
                right = self._parse_power()
                left = ("binop", op, left, right)
            else:
                break
        return left

    def _parse_power(self) -> Any:
        left = self._parse_unary()
        t = self._peek()
        if t and t[0] == "OP" and t[1] == "^":
            self._eat()
            right = self._parse_power()
            return ("binop", "^", left, right)
        return left

    def _parse_unary(self) -> Any:
        t = self._peek()
        if t and t[0] == "OP" and t[1] in ("+", "-", "@"):
            op = self._eat()[1]
            sub = self._parse_unary()
            return ("unop", op, sub)
        node = self._parse_primary()
        t2 = self._peek()
        if t2 and t2[0] == "OP" and t2[1] == "%":
            self._eat()
            node = ("unop", "%", node)
        return node

    def _parse_primary(self) -> Any:
        t = self._peek()
        if not t:
            return ("lit", "")
        kind, val = t

        if kind == "STR":
            self._eat()
            return ("lit", val[1:-1].replace('""', '"'))

        if kind == "ROWREF":
            nxt_r = self._peek(1)
            if nxt_r and nxt_r[0] == "OP" and nxt_r[1] == ":":
                return self._parse_ref_after_sheet(None)
            self._eat()
            return ("lit", "#REF!")

        if kind == "NUM":
            nxt_n = self._peek(1)
            if val.isdigit() and nxt_n and nxt_n[0] == "OP" and nxt_n[1] == ":":
                return self._parse_ref_after_sheet(None)
            self._eat()
            fval = float(val)
            return (
                "lit",
                int(fval)
                if fval.is_integer() and "." not in val and "e" not in val.lower()
                else fval,
            )

        if kind == "ERR":
            self._eat()
            return ("lit", val.upper())

        if kind == "SREF":
            self._eat()
            b_idx = val.find("[")
            tbl_name = val[:b_idx].strip() if b_idx > 0 else ""
            spec = val[b_idx:] if b_idx >= 0 else val
            return ("sref", tbl_name or None, spec)

        if kind == "OP" and val == "{":
            self._eat()
            arr_rows: List[List[Any]] = []
            cur_row_nodes: List[Any] = []
            while self._peek() and not (self._peek()[0] == "OP" and self._peek()[1] == "}"):
                cur_row_nodes.append(self._parse_comparison())
                sep = self._peek()
                if sep and sep[0] == "OP" and sep[1] == ",":
                    self._eat()
                elif sep and sep[0] == "OP" and sep[1] == ";":
                    self._eat()
                    arr_rows.append(cur_row_nodes)
                    cur_row_nodes = []
                else:
                    break
            if cur_row_nodes or not arr_rows:
                arr_rows.append(cur_row_nodes or [("lit", "")])
            if self._peek() and self._peek()[0] == "OP" and self._peek()[1] == "}":
                self._eat()
            return ("arr_lit", arr_rows)

        if kind == "OP" and val == "(":
            self._eat()
            inner = self._parse_comparison()
            nxt = self._peek()
            if nxt and nxt[0] == "OP" and nxt[1] == ")":
                self._eat()
            return inner

        if kind in ("QSHEET", "USHEET"):
            sh_tok = self._eat()
            sheet_name = _unquote_sheet_token(sh_tok[0], sh_tok[1])
            return self._parse_ref_after_sheet(sheet_name)

        if kind == "IDENT":
            nxt = self._peek(1)
            if nxt and nxt[0] == "OP" and nxt[1] == "(":
                fn_name = self._eat()[1].upper()
                for prefix in ("_XLFN.", "_XLWS.", "_XLUDF."):
                    if fn_name.startswith(prefix):
                        fn_name = fn_name[len(prefix) :]
                self._eat()  # '('
                args: List[Any] = []
                if not (self._peek() and self._peek()[0] == "OP" and self._peek()[1] == ")"):
                    while True:
                        cur = self._peek()
                        if cur and cur[0] == "OP" and cur[1] in (",", ";"):
                            args.append(("lit", None))
                        else:
                            args.append(self._parse_comparison())
                        sep = self._peek()
                        if sep and sep[0] == "OP" and sep[1] in (",", ";"):
                            self._eat()
                            if self._peek() and self._peek()[0] == "OP" and self._peek()[1] == ")":
                                args.append(("lit", None))
                                break
                        else:
                            break
                if self._peek() and self._peek()[0] == "OP" and self._peek()[1] == ")":
                    self._eat()
                return ("call", fn_name, args)

            up = val.upper()
            if up == "TRUE":
                self._eat()
                return ("lit", True)
            if up == "FALSE":
                self._eat()
                return ("lit", False)

            return self._parse_ref_after_sheet(None)

        raise ValueError(f"意外的公式标记: {t!r}")

    def _parse_ref_after_sheet(self, sheet_name: Optional[str]) -> Any:
        t1 = self._eat()
        first_str = t1[1]
        nxt = self._peek()
        if nxt and nxt[0] == "OP" and nxt[1] == ":":
            self._eat()  # ':'
            # 允许 Sheet1!A1:Sheet1!B2 写法
            if self._peek() and self._peek()[0] in ("QSHEET", "USHEET"):
                self._eat()
            t2 = self._eat()
            second_str = t2[1]
            c1_rc = _parse_coord(first_str)
            c2_rc = _parse_coord(second_str)
            if c1_rc and c2_rc:
                return ("range", sheet_name, c1_rc[0], c1_rc[1], c2_rc[0], c2_rc[1], False)
            m1 = _COL_ONLY_RE.match(first_str)
            m2 = _COL_ONLY_RE.match(second_str)
            if m1 and m2:
                col1 = column_index_from_string(m1.group(1).upper())
                col2 = column_index_from_string(m2.group(1).upper())
                return ("range", sheet_name, 1, col1, 1048576, col2, True)
            r1_raw = first_str.lstrip("$")
            r2_raw = second_str.lstrip("$")
            if r1_raw.isdigit() and r2_raw.isdigit():
                r1_i, r2_i = int(r1_raw), int(r2_raw)
                return ("range", sheet_name, r1_i, 1, r2_i, 16384, False)
            return ("lit", "#REF!")

        rc = _parse_coord(first_str)
        if rc:
            return ("cell", sheet_name, rc[0], rc[1])
        return ("name", sheet_name, first_str)


def _norm_lookup_key(val: Any) -> Any:
    """将查找键归一化：字符串忽略大小写，整数浮点 100.0 与 100 统一为 int."""
    if val is None:
        return None
    if isinstance(val, bool):
        return ("b", val)
    if isinstance(val, int):
        return ("n", val)
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
        return ("n", int(val) if val.is_integer() else val)
    s = _ZERO_WIDTH_RE.sub("", str(val)).strip()
    if is_excel_error_value(s):
        return ("err", unicodedata.normalize("NFKC", s).upper())
    return ("s", s.lower())


def _to_number(val: Any) -> Optional[float]:
    if val is None or val == "":
        return 0.0
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).strip())
    except Exception:
        return None


def _to_excel_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _is_truthy_excel(val: Any) -> bool:
    if val is None or val == "" or val is False or val == 0:
        return False
    if isinstance(val, str) and val.strip().upper() == "FALSE":
        return False
    return True


def _is_na_error(val: Any) -> bool:
    if not is_excel_error_value(val):
        return False
    norm = unicodedata.normalize("NFKC", str(val)).strip().lstrip("'\"").rstrip("'\"").upper()
    return norm in ("#N/A", "#N/A!", "#NA")


def _compare_excel_values(a: Any, b: Any, op: str) -> Any:
    if is_excel_error_value(a):
        return str(a).upper()
    if is_excel_error_value(b):
        return str(b).upper()
    a_norm = "" if a is None else a
    b_norm = "" if b is None else b

    # 数值与数值比较
    if (
        isinstance(a_norm, (int, float))
        and not isinstance(a_norm, bool)
        and isinstance(b_norm, (int, float))
        and not isinstance(b_norm, bool)
    ):
        ca, cb = float(a_norm), float(b_norm)
    elif isinstance(a_norm, str) and isinstance(b_norm, str):
        ca, cb = a_norm.lower(), b_norm.lower()
    else:
        # 异构类型（如空串 "" 与 0）
        if op == "=":
            return _norm_lookup_key(a_norm) == _norm_lookup_key(b_norm)
        if op == "<>":
            return _norm_lookup_key(a_norm) != _norm_lookup_key(b_norm)
        ca, cb = str(a_norm).lower(), str(b_norm).lower()

    if op == "=":
        return ca == cb
    if op == "<>":
        return ca != cb
    if op == "<":
        return ca < cb
    if op == "<=":
        return ca <= cb
    if op == ">":
        return ca > cb
    if op == ">=":
        return ca >= cb
    return False


def _eval_scalar_binop(op: str, lv: Any, rv: Any) -> Any:
    if op in ("=", "<>", "<", "<=", ">", ">="):
        return _compare_excel_values(lv, rv, op)

    if is_excel_error_value(lv):
        return str(lv).upper()
    if is_excel_error_value(rv):
        return str(rv).upper()

    if op == "&":
        return _to_excel_str(lv) + _to_excel_str(rv)

    ln = _to_number(lv)
    rn = _to_number(rv)
    if ln is None or rn is None:
        return "#VALUE!"
    if op == "+":
        out = ln + rn
    elif op == "-":
        out = ln - rn
    elif op == "*":
        out = ln * rn
    elif op == "/":
        if rn == 0:
            return "#DIV/0!"
        out = ln / rn
    elif op == "^":
        out = ln**rn
    else:
        return "#VALUE!"
    return int(out) if float(out).is_integer() else out


class WorkbookFormulaEvaluator:
    """跨工作表公式求值引擎（只读流式值矩阵 + 稀疏 XML 公式映射 + $O(1)$ 列哈希索引）."""

    def __init__(
        self,
        content: bytes,
        cached_empty_by_sheet: Optional[Dict[str, Set[Tuple[int, int]]]] = None,
        *,
        package_scan: Optional[_WorkbookPackageScan] = None,
    ) -> None:
        scan = package_scan or _scan_xlsx_package(content)
        wb_bytes = scan.normalized_content or content
        # 采用 read_only=True 打开缓存值工作簿：零预加载 Cell 对象，26,000x46 大表初始化 < 0.05s
        self._wb_val = openpyxl.load_workbook(
            io.BytesIO(wb_bytes),
            read_only=True,
            data_only=True,
        )
        self._val_matrices: Dict[str, List[Tuple[Any, ...]]] = {}
        self._sheet_name_map: Dict[str, str] = {
            t.lower(): t for t in self._wb_val.sheetnames
        }

        raw_empty = cached_empty_by_sheet if cached_empty_by_sheet is not None else scan.cached_empty_by_title
        self._cached_empty_by_sheet: Dict[str, Set[Tuple[int, int]]] = {}
        for sh_key, coords in (raw_empty or {}).items():
            resolved = self._resolve_sheet_key(sh_key)
            if resolved:
                self._cached_empty_by_sheet[resolved] = coords

        self._formulas_by_sheet: Dict[str, Dict[Tuple[int, int], str]] = {}
        self._fml_max_dims: Dict[str, Tuple[int, int]] = {}
        for sh_key, fmap in scan.formulas_by_title.items():
            resolved = self._resolve_sheet_key(sh_key)
            if resolved and fmap:
                self._formulas_by_sheet[resolved] = fmap
                max_r = max(rc[0] for rc in fmap.keys())
                max_c = max(rc[1] for rc in fmap.keys())
                self._fml_max_dims[resolved] = (max_r, max_c)

        self._defined_names: Dict[Tuple[Optional[str], str], str] = dict(scan.defined_names)
        self._tables_meta: Dict[str, Dict[str, Any]] = dict(scan.tables_meta)

        self._ast_cache: Dict[str, Any] = {}
        self._eval_cache: Dict[Tuple[str, int, int], Any] = {}
        self._visiting: Set[Tuple[str, int, int]] = set()
        self._lookup_index_cache: Dict[Tuple[str, int, int, int], Dict[Any, int]] = {}

    def _resolve_sheet_key(self, sh_key: str) -> Optional[str]:
        if sh_key in self._wb_val.sheetnames:
            return sh_key
        low = sh_key.strip().lower()
        if low in self._sheet_name_map:
            return self._sheet_name_map[low]
        for idx, title in enumerate(self._wb_val.sheetnames, start=1):
            if sh_key == f"xl/worksheets/sheet{idx}.xml":
                return title
        return None

    def close(self) -> None:
        try:
            self._wb_val.close()
        except Exception:
            pass

    def _resolve_sheet_title(self, name: Optional[str], fallback: str) -> Optional[str]:
        if name == "__EXTERNAL_WB__":
            return None
        if not name:
            return fallback if fallback in self._wb_val.sheetnames else self._sheet_name_map.get(fallback.lower())
        if name in self._wb_val.sheetnames:
            return name
        return self._sheet_name_map.get(name.strip().lower())

    def _get_val_matrix(self, sheet: str) -> List[Tuple[Any, ...]]:
        mat = self._val_matrices.get(sheet)
        if mat is None:
            ws = self._wb_val[sheet]
            if hasattr(ws, "reset_dimensions"):
                try:
                    ws.reset_dimensions()
                except Exception:
                    pass
            mat = list(ws.iter_rows(values_only=True))
            self._val_matrices[sheet] = mat
        return mat

    def _sheet_max_row(self, sheet: str) -> int:
        v_len = len(self._get_val_matrix(sheet))
        f_r = self._fml_max_dims.get(sheet, (0, 0))[0]
        return max(v_len, f_r)

    def _sheet_row_width(self, sheet: str, row_1b: int, val_mat: List[Tuple[Any, ...]]) -> int:
        w_v = len(val_mat[row_1b - 1]) if row_1b <= len(val_mat) else 0
        f_c = self._fml_max_dims.get(sheet, (0, 0))[1]
        return max(w_v, f_c)

    def get_cell_value(self, sheet: str, row_1b: int, col_1b: int, depth: int = 0) -> Any:
        """获取指定 1-based 单元格最终值（缓存值优先，缺失缓存时求值公式）."""
        if depth > 64 or row_1b < 1 or col_1b < 1:
            return "#REF!"
        real_sheet = self._resolve_sheet_title(sheet, sheet)
        if not real_sheet:
            return "#REF!"

        key = (real_sheet, row_1b, col_1b)
        if key in self._eval_cache:
            return self._eval_cache[key]
        if key in self._visiting:
            return "#REF!"

        # 1. 若 XML 中已显式缓存空字符串 (<c t="str"><f>...</f><v/></c>)，直接采用 Excel 最终结果 ""
        if (row_1b, col_1b) in self._cached_empty_by_sheet.get(real_sheet, ()):
            self._eval_cache[key] = ""
            return ""

        val_mat = self._get_val_matrix(real_sheet)
        cached_val: Any = None
        if row_1b <= len(val_mat):
            v_row = val_mat[row_1b - 1]
            if col_1b <= len(v_row):
                cached_val = v_row[col_1b - 1]

        # 2. 若已有非空缓存值且不是文本态公式，直接采用缓存值
        if cached_val is not None:
            if isinstance(cached_val, str) and _TEXT_FORMULA_RE.match(cached_val.strip()):
                self._visiting.add(key)
                try:
                    res = self.evaluate_formula_string(
                        cached_val.strip(), real_sheet, depth + 1, cur_row=row_1b, cur_col=col_1b
                    )
                finally:
                    self._visiting.discard(key)
                self._eval_cache[key] = res
                return res
            self._eval_cache[key] = cached_val
            return cached_val

        # 3. 检查稀疏公式表是否存在未缓存公式
        fml_expr = self._formulas_by_sheet.get(real_sheet, {}).get((row_1b, col_1b))
        if not fml_expr:
            self._eval_cache[key] = None
            return None

        self._visiting.add(key)
        try:
            res = self._eval_formula_expr(
                fml_expr, real_sheet, depth + 1, cur_row=row_1b, cur_col=col_1b
            )
        except Exception:
            res = "#VALUE!"
        finally:
            self._visiting.discard(key)

        self._eval_cache[key] = res
        return res

    def evaluate_formula_string(
        self,
        formula_with_eq: str,
        current_sheet: str,
        depth: int = 0,
        *,
        cur_row: int = 1,
        cur_col: int = 1,
    ) -> Any:
        s = (formula_with_eq or "").strip()
        if not s.startswith("="):
            return formula_with_eq
        try:
            return self._eval_formula_expr(
                s[1:].strip(), current_sheet, depth, cur_row=cur_row, cur_col=cur_col
            )
        except Exception:
            # 若无法按公式解析（如普通文本 "=1:有效"），保留原文本
            return formula_with_eq

    def _intersect_range_scalar(
        self, rng: _RangeValue, cur_row: int, cur_col: int, depth: int
    ) -> Any:
        if rng.c1 == rng.c2 and rng.r2 > rng.r1 and rng.r1 <= cur_row <= rng.r2:
            return self.get_cell_value(rng.sheet, cur_row, rng.c1, depth + 1)
        if rng.r1 == rng.r2 and rng.c2 > rng.c1 and rng.c1 <= cur_col <= rng.c2:
            return self.get_cell_value(rng.sheet, rng.r1, cur_col, depth + 1)
        return self.get_cell_value(rng.sheet, rng.r1, rng.c1, depth + 1)

    def _intersect_array_scalar(self, arr: _ArrayValue, cur_row: int, cur_col: int) -> Any:
        if not arr.rows or not arr.rows[0]:
            return ""
        if arr.width == 1 and arr.height > 1:
            offset = cur_row - arr.origin_r
            if 0 <= offset < arr.height:
                return arr.rows[offset][0]
        if arr.height == 1 and arr.width > 1:
            offset = cur_col - arr.origin_c
            if 0 <= offset < arr.width:
                return arr.rows[0][offset]
        return arr.rows[0][0]

    def _parse_expr_ast(self, expr: str) -> Any:
        ast = self._ast_cache.get(expr)
        if ast is None:
            tokens = _tokenize_formula(expr)
            ast = _FormulaParser(tokens).parse()
            if len(self._ast_cache) < 4096:
                self._ast_cache[expr] = ast
        return ast

    def _eval_formula_expr(
        self,
        expr: str,
        current_sheet: str,
        depth: int,
        *,
        cur_row: int = 1,
        cur_col: int = 1,
    ) -> Any:
        ast = self._parse_expr_ast(expr)
        val = self._eval_node(ast, current_sheet, depth, cur_row, cur_col)
        if isinstance(val, _RangeValue):
            return self._intersect_range_scalar(val, cur_row, cur_col, depth)
        if isinstance(val, _ArrayValue):
            return self._intersect_array_scalar(val, cur_row, cur_col)
        return val

    def _sheet_max_col(self, sheet: str) -> int:
        val_mat = self._get_val_matrix(sheet)
        v_w = max((len(r) for r in val_mat), default=0)
        f_c = self._fml_max_dims.get(sheet, (0, 0))[1]
        return max(v_w, f_c)

    def _bound_range(self, rng: _RangeValue) -> Tuple[str, int, int, int, int]:
        max_r = self._sheet_max_row(rng.sheet)
        if max_r < 1:
            return (rng.sheet, 1, rng.c1, 0, rng.c2)
        eff_r2 = min(rng.r2, max_r) if rng.whole_col or rng.r2 > max_r else rng.r2
        eff_c2 = rng.c2
        if rng.c2 >= 16384:
            max_c = self._sheet_max_col(rng.sheet)
            eff_c2 = max(rng.c1, min(rng.c2, max_c)) if max_c > 0 else rng.c1
        return (rng.sheet, rng.r1, rng.c1, eff_r2, eff_c2)

    def _materialize_array(self, val: Any, depth: int) -> Optional[_ArrayValue]:
        if isinstance(val, _ArrayValue):
            return val
        if isinstance(val, _RangeValue):
            sh, r1, c1, r2, c2 = self._bound_range(val)
            if r2 < r1 or c2 < c1:
                return _ArrayValue([[""]], r1, c1)
            cell_cnt = (r2 - r1 + 1) * (c2 - c1 + 1)
            if cell_cnt > _MAX_ARRAY_CELLS:
                return None
            rows = [
                [self.get_cell_value(sh, r, c, depth + 1) for c in range(c1, c2 + 1)]
                for r in range(r1, r2 + 1)
            ]
            return _ArrayValue(rows, r1, c1)
        return None

    def _eval_array_binop(
        self, op: str, left_arr: Optional[_ArrayValue], right_arr: Optional[_ArrayValue], lv: Any, rv: Any
    ) -> Any:
        h = max(left_arr.height if left_arr else 1, right_arr.height if right_arr else 1)
        w = max(left_arr.width if left_arr else 1, right_arr.width if right_arr else 1)
        if h * w > _MAX_ARRAY_CELLS:
            return "#VALUE!"
        origin_r = left_arr.origin_r if left_arr else (right_arr.origin_r if right_arr else 1)
        origin_c = left_arr.origin_c if left_arr else (right_arr.origin_c if right_arr else 1)

        def _at(arr: Optional[_ArrayValue], scalar: Any, r_i: int, c_i: int) -> Any:
            if arr is None:
                return scalar
            if arr.height == 1 and arr.width == 1:
                return arr.rows[0][0]
            if arr.height == 1 and c_i < arr.width:
                return arr.rows[0][c_i]
            if arr.width == 1 and r_i < arr.height:
                return arr.rows[r_i][0]
            if r_i < arr.height and c_i < arr.width:
                return arr.rows[r_i][c_i]
            return "#N/A"

        out_rows = [
            [
                _eval_scalar_binop(op, _at(left_arr, lv, r_i, c_i), _at(right_arr, rv, r_i, c_i))
                for c_i in range(w)
            ]
            for r_i in range(h)
        ]
        return _ArrayValue(out_rows, origin_r, origin_c)

    def _resolve_defined_name_or_table(
        self,
        sheet_opt: Optional[str],
        name_str: str,
        current_sheet: str,
        depth: int,
        cur_row: int,
        cur_col: int,
    ) -> Any:
        if depth > 64:
            return "#REF!"
        name_low = name_str.strip().lower()
        scope_sheet = self._resolve_sheet_title(sheet_opt, current_sheet) if sheet_opt else current_sheet
        scope_low = scope_sheet.lower() if scope_sheet else None

        dn_expr = None
        if scope_low:
            dn_expr = self._defined_names.get((scope_low, name_low))
        if dn_expr is None and not sheet_opt:
            dn_expr = self._defined_names.get((None, name_low))
        if dn_expr is not None:
            try:
                dn_ast = self._parse_expr_ast(dn_expr)
                return self._eval_node(dn_ast, scope_sheet or current_sheet, depth + 1, cur_row, cur_col)
            except Exception:
                return "#NAME?"

        # 兼容直接将 Excel 超级表名作为区域引用（如 VLOOKUP(A2, TblDict, 2, 0)）
        if not sheet_opt and name_low in self._tables_meta:
            tinfo = self._tables_meta[name_low]
            dr1 = tinfo["r1"] + tinfo["header_rows"]
            dr2 = max(dr1, tinfo["r2"] - tinfo["totals_rows"])
            return _RangeValue(tinfo["sheet"], dr1, tinfo["c1"], dr2, tinfo["c2"], False)

        return "#NAME?"

    def _resolve_structured_ref(
        self,
        table_name: Optional[str],
        spec: str,
        current_sheet: str,
        depth: int,
        cur_row: int,
        cur_col: int,
    ) -> Any:
        tinfo: Optional[Dict[str, Any]] = None
        if table_name:
            tinfo = self._tables_meta.get(table_name.strip().lower())
        else:
            for cand in self._tables_meta.values():
                if (
                    cand["sheet"].lower() == current_sheet.lower()
                    and cand["r1"] <= cur_row <= cand["r2"]
                    and cand["c1"] <= cur_col <= cand["c2"]
                ):
                    tinfo = cand
                    break
        if not tinfo:
            return "#REF!"

        inner = spec.strip()
        if inner.startswith("[") and inner.endswith("]"):
            inner = inner[1:-1].strip()

        def _unescape_col(raw_c: str) -> str:
            s = raw_c.strip()
            if s.startswith("[") and s.endswith("]"):
                s = s[1:-1].strip()
            return re.sub(r"'(.)", r"\1", s).lower()

        sh = tinfo["sheet"]
        r1, c1, r2, c2 = tinfo["r1"], tinfo["c1"], tinfo["r2"], tinfo["c2"]
        data_r1 = r1 + tinfo["header_rows"]
        data_r2 = max(data_r1, r2 - tinfo["totals_rows"])
        cols_map: Dict[str, int] = tinfo["columns"]

        # 当前行引用：[@列名] 或 [[#This Row],[列名]]
        if inner.startswith("@"):
            col_key = _unescape_col(inner[1:])
            if not col_key:
                return _RangeValue(sh, cur_row, c1, cur_row, c2, False)
            col_idx = cols_map.get(col_key)
            if col_idx is None:
                return "#REF!"
            return self.get_cell_value(sh, cur_row, col_idx, depth + 1)

        low_inner = inner.lower()
        if "#this row" in low_inner:
            parts = [p.strip() for p in inner.split(",") if "#this row" not in p.lower()]
            if parts:
                col_key = _unescape_col(parts[-1])
                col_idx = cols_map.get(col_key)
                if col_idx is not None:
                    return self.get_cell_value(sh, cur_row, col_idx, depth + 1)
            return "#REF!"

        if low_inner == "#all":
            return _RangeValue(sh, r1, c1, r2, c2, False)
        if low_inner == "#data" or inner == "":
            return _RangeValue(sh, data_r1, c1, data_r2, c2, False)
        if low_inner == "#headers":
            return _RangeValue(sh, r1, c1, r1, c2, False)

        use_r1, use_r2 = data_r1, data_r2
        if "#all" in low_inner:
            use_r1, use_r2 = r1, r2
            parts = [p.strip() for p in inner.split(",") if "#all" not in p.lower()]
            inner = parts[-1] if parts else ""
        elif "#data" in low_inner:
            parts = [p.strip() for p in inner.split(",") if "#data" not in p.lower()]
            inner = parts[-1] if parts else ""

        if ":" in inner:
            p_left, p_right = inner.split(":", 1)
            ci1 = cols_map.get(_unescape_col(p_left))
            ci2 = cols_map.get(_unescape_col(p_right))
            if ci1 is None or ci2 is None:
                return "#REF!"
            return _RangeValue(sh, use_r1, min(ci1, ci2), use_r2, max(ci1, ci2), False)

        col_idx = cols_map.get(_unescape_col(inner))
        if col_idx is None:
            return "#REF!"
        return _RangeValue(sh, use_r1, col_idx, use_r2, col_idx, False)

    def _get_exact_col_index(
        self, sheet: str, r1: int, r2: int, col_1b: int, depth: int
    ) -> Dict[Any, int]:
        cache_key = (sheet, r1, r2, col_1b)
        idx_map = self._lookup_index_cache.get(cache_key)
        if idx_map is not None:
            return idx_map
        idx_map = {}
        for r in range(r1, r2 + 1):
            v = self.get_cell_value(sheet, r, col_1b, depth + 1)
            if v is None or v == "" or is_excel_error_value(v):
                continue
            nk = _norm_lookup_key(v)
            if nk is not None and nk not in idx_map:
                idx_map[nk] = r
        self._lookup_index_cache[cache_key] = idx_map
        return idx_map

    def _iter_arg_scalars(
        self,
        args: Sequence[Any],
        current_sheet: str,
        depth: int,
        cur_row: int = 1,
        cur_col: int = 1,
    ) -> Iterator[Any]:
        for arg in args:
            v = self._eval_node(arg, current_sheet, depth, cur_row, cur_col)
            if isinstance(v, _RangeValue):
                sh, r1, c1, r2, c2 = self._bound_range(v)
                for r in range(r1, r2 + 1):
                    for c in range(c1, c2 + 1):
                        yield self.get_cell_value(sh, r, c, depth + 1)
            elif isinstance(v, _ArrayValue):
                for row_vals in v.rows:
                    for item in row_vals:
                        yield item
            else:
                yield v

    def _eval_node(
        self, node: Any, current_sheet: str, depth: int, cur_row: int = 1, cur_col: int = 1
    ) -> Any:
        ntype = node[0]
        if ntype == "lit":
            return node[1]

        if ntype == "arr_lit":
            _, row_nodes = node
            out_rows = [
                [self._eval_scalar(el_n, current_sheet, depth + 1, cur_row, cur_col) for el_n in r_n]
                for r_n in row_nodes
            ]
            return _ArrayValue(out_rows, 1, 1)

        if ntype == "cell":
            _, sh_opt, r, c = node
            target_sh = self._resolve_sheet_title(sh_opt, current_sheet)
            if not target_sh:
                return "#REF!"
            return self.get_cell_value(target_sh, r, c, depth + 1)

        if ntype == "range":
            _, sh_opt, r1, c1, r2, c2, whole_col = node
            target_sh = self._resolve_sheet_title(sh_opt, current_sheet)
            if not target_sh:
                return "#REF!"
            return _RangeValue(target_sh, r1, c1, r2, c2, whole_col)

        if ntype == "name":
            _, sh_opt, name_str = node
            return self._resolve_defined_name_or_table(
                sh_opt, name_str, current_sheet, depth, cur_row, cur_col
            )

        if ntype == "sref":
            _, tbl_name, spec = node
            return self._resolve_structured_ref(
                tbl_name, spec, current_sheet, depth, cur_row, cur_col
            )

        if ntype == "unop":
            _, op, sub = node
            v = self._eval_node(sub, current_sheet, depth, cur_row, cur_col)
            if op == "@":
                if isinstance(v, _RangeValue):
                    return self._intersect_range_scalar(v, cur_row, cur_col, depth)
                if isinstance(v, _ArrayValue):
                    return self._intersect_array_scalar(v, cur_row, cur_col)
                return v
            if isinstance(v, _RangeValue):
                v = self._intersect_range_scalar(v, cur_row, cur_col, depth)
            elif isinstance(v, _ArrayValue):
                v = self._intersect_array_scalar(v, cur_row, cur_col)
            if is_excel_error_value(v):
                return str(v).upper()
            num = _to_number(v)
            if num is None:
                return "#VALUE!"
            if op == "-":
                res = -num
            elif op == "%":
                res = num / 100.0
            else:
                res = num
            return int(res) if float(res).is_integer() else res

        if ntype == "binop":
            _, op, left_n, right_n = node
            lv = self._eval_node(left_n, current_sheet, depth, cur_row, cur_col)
            rv = self._eval_node(right_n, current_sheet, depth, cur_row, cur_col)
            if isinstance(lv, (_RangeValue, _ArrayValue)) or isinstance(rv, (_RangeValue, _ArrayValue)):
                l_arr = self._materialize_array(lv, depth)
                r_arr = self._materialize_array(rv, depth)
                if (isinstance(lv, (_RangeValue, _ArrayValue)) and l_arr is None) or (
                    isinstance(rv, (_RangeValue, _ArrayValue)) and r_arr is None
                ):
                    if isinstance(lv, _RangeValue):
                        lv = self._intersect_range_scalar(lv, cur_row, cur_col, depth)
                    if isinstance(rv, _RangeValue):
                        rv = self._intersect_range_scalar(rv, cur_row, cur_col, depth)
                    return _eval_scalar_binop(op, lv, rv)
                return self._eval_array_binop(op, l_arr, r_arr, lv, rv)

            return _eval_scalar_binop(op, lv, rv)

        if ntype == "call":
            _, fname, args = node
            return self._eval_func(fname, args, current_sheet, depth, cur_row, cur_col)

        return ""

    def _eval_scalar(
        self, node: Any, current_sheet: str, depth: int, cur_row: int = 1, cur_col: int = 1
    ) -> Any:
        v = self._eval_node(node, current_sheet, depth, cur_row, cur_col)
        if isinstance(v, _RangeValue):
            return self._intersect_range_scalar(v, cur_row, cur_col, depth)
        if isinstance(v, _ArrayValue):
            return self._intersect_array_scalar(v, cur_row, cur_col)
        return v

    @staticmethod
    def _flatten_1d_array(arr: _ArrayValue) -> Optional[List[Any]]:
        if arr.width == 1:
            return [r[0] for r in arr.rows]
        if arr.height == 1:
            return list(arr.rows[0]) if arr.rows else []
        return None

    def _eval_func(
        self,
        fname: str,
        args: List[Any],
        current_sheet: str,
        depth: int,
        cur_row: int = 1,
        cur_col: int = 1,
    ) -> Any:
        # 1. 逻辑与常量/坐标辅助函数（惰性求值）
        if fname == "NA":
            return "#N/A"
        if fname == "TRUE":
            return True
        if fname == "FALSE":
            return False
        if fname == "SINGLE":
            return self._eval_scalar(args[0], current_sheet, depth, cur_row, cur_col) if args else ""
        if fname == "ROW":
            if not args:
                return cur_row
            ref = args[0]
            if ref[0] == "cell":
                return ref[2]
            if ref[0] == "range":
                return ref[2]
            return cur_row
        if fname == "COLUMN":
            if not args:
                return cur_col
            ref = args[0]
            if ref[0] == "cell":
                return ref[3]
            if ref[0] == "range":
                return ref[3]
            return cur_col

        def _sc(arg_node: Any) -> Any:
            return self._eval_scalar(arg_node, current_sheet, depth, cur_row, cur_col)

        def _nd(arg_node: Any) -> Any:
            return self._eval_node(arg_node, current_sheet, depth, cur_row, cur_col)

        def _sc_iter(arg_list: Sequence[Any]) -> Iterator[Any]:
            return self._iter_arg_scalars(arg_list, current_sheet, depth, cur_row, cur_col)

        if fname == "IF":
            if not args:
                return False
            cond_raw = _nd(args[0])
            if isinstance(cond_raw, _ArrayValue):
                t_val = _nd(args[1]) if len(args) > 1 else True
                f_val = _nd(args[2]) if len(args) > 2 else False
                t_arr = self._materialize_array(t_val, depth)
                f_arr = self._materialize_array(f_val, depth)
                out_rows: List[List[Any]] = []
                for r_i, c_row in enumerate(cond_raw.rows):
                    row_out: List[Any] = []
                    for c_i, cv in enumerate(c_row):
                        if is_excel_error_value(cv):
                            row_out.append(str(cv).upper())
                        elif _is_truthy_excel(cv):
                            row_out.append(
                                t_arr.rows[min(r_i, t_arr.height - 1)][min(c_i, t_arr.width - 1)]
                                if t_arr and t_arr.rows
                                else t_val
                            )
                        else:
                            row_out.append(
                                f_arr.rows[min(r_i, f_arr.height - 1)][min(c_i, f_arr.width - 1)]
                                if f_arr and f_arr.rows
                                else f_val
                            )
                    out_rows.append(row_out)
                return _ArrayValue(out_rows, cond_raw.origin_r, cond_raw.origin_c)

            cond = (
                self._intersect_range_scalar(cond_raw, cur_row, cur_col, depth)
                if isinstance(cond_raw, _RangeValue)
                else cond_raw
            )
            if is_excel_error_value(cond):
                return str(cond).upper()
            if _is_truthy_excel(cond):
                return _sc(args[1]) if len(args) > 1 else True
            return _sc(args[2]) if len(args) > 2 else False

        if fname == "IFS":
            for i in range(0, len(args) - 1, 2):
                cond = _sc(args[i])
                if is_excel_error_value(cond):
                    return str(cond).upper()
                if _is_truthy_excel(cond):
                    return _sc(args[i + 1])
            return "#N/A"

        if fname == "IFERROR":
            if len(args) < 2:
                return "#VALUE!"
            val = _sc(args[0])
            if is_excel_error_value(val):
                return _sc(args[1])
            return val

        if fname == "IFNA":
            if len(args) < 2:
                return "#VALUE!"
            val = _sc(args[0])
            if _is_na_error(val):
                return _sc(args[1])
            return val

        if fname in ("ISNA", "ISERROR", "ISERR", "ISBLANK", "ISNUMBER", "ISTEXT"):
            if not args:
                return False
            v = _sc(args[0])
            if fname == "ISNA":
                return _is_na_error(v)
            if fname == "ISERROR":
                return is_excel_error_value(v)
            if fname == "ISERR":
                return is_excel_error_value(v) and not _is_na_error(v)
            if fname == "ISBLANK":
                return v is None or v == ""
            if fname == "ISNUMBER":
                return isinstance(v, (int, float)) and not isinstance(v, bool)
            if fname == "ISTEXT":
                return isinstance(v, str) and not is_excel_error_value(v)

        if fname == "AND":
            for v in _sc_iter(args):
                if is_excel_error_value(v):
                    return str(v).upper()
                if not _is_truthy_excel(v):
                    return False
            return True

        if fname == "OR":
            for v in _sc_iter(args):
                if is_excel_error_value(v):
                    return str(v).upper()
                if _is_truthy_excel(v):
                    return True
            return False

        if fname == "NOT":
            if not args:
                return "#VALUE!"
            v = _sc(args[0])
            if is_excel_error_value(v):
                return str(v).upper()
            return not _is_truthy_excel(v)

        # 2. 查找引用函数 (VLOOKUP / HLOOKUP / XLOOKUP / MATCH / INDEX)
        if fname == "VLOOKUP":
            if len(args) < 3:
                return "#VALUE!"
            lookup_val = _sc(args[0])
            if is_excel_error_value(lookup_val):
                return str(lookup_val).upper()
            if lookup_val is None or lookup_val == "":
                return "#N/A"

            tbl_rng = _nd(args[1])
            col_num_raw = _to_number(_sc(args[2]))
            if col_num_raw is None or int(col_num_raw) < 1:
                return "#VALUE!"
            col_offset = int(col_num_raw) - 1

            approx = True
            if len(args) >= 4 and args[3] != ("lit", None):
                rl_val = _sc(args[3])
                approx = _is_truthy_excel(rl_val)

            nk = _norm_lookup_key(lookup_val)
            if isinstance(tbl_rng, _ArrayValue):
                if col_offset >= tbl_rng.width:
                    return "#REF!"
                if not tbl_rng.rows:
                    return "#N/A"
                if not approx:
                    for r_row in tbl_rng.rows:
                        cv = r_row[0] if r_row else None
                        if cv is None or cv == "" or is_excel_error_value(cv):
                            continue
                        if _norm_lookup_key(cv) == nk:
                            out = r_row[col_offset]
                            return "" if out is None else out
                    return "#N/A"
                best_arr_row: Optional[List[Any]] = None
                for r_row in tbl_rng.rows:
                    cv = r_row[0] if r_row else None
                    if cv is None or cv == "" or is_excel_error_value(cv):
                        continue
                    if _compare_excel_values(cv, lookup_val, "<="):
                        best_arr_row = r_row
                    else:
                        break
                if best_arr_row is None:
                    return "#N/A"
                out = best_arr_row[col_offset]
                return "" if out is None else out

            if not isinstance(tbl_rng, _RangeValue):
                return "#REF!" if tbl_rng == "#REF!" else "#N/A"

            sh, r1, c1, r2, c2 = self._bound_range(tbl_rng)
            target_col = c1 + col_offset
            if target_col > c2 or r2 < r1:
                return "#REF!" if target_col > c2 else "#N/A"

            if not approx:
                idx_map = self._get_exact_col_index(sh, r1, r2, c1, depth)
                hit_row = idx_map.get(nk)
                if hit_row is None:
                    return "#N/A"
                out = self.get_cell_value(sh, hit_row, target_col, depth + 1)
                return "" if out is None else out

            best_row: Optional[int] = None
            for r in range(r1, r2 + 1):
                cv = self.get_cell_value(sh, r, c1, depth + 1)
                if cv is None or cv == "" or is_excel_error_value(cv):
                    continue
                if _compare_excel_values(cv, lookup_val, "<="):
                    best_row = r
                else:
                    break
            if best_row is None:
                return "#N/A"
            out = self.get_cell_value(sh, best_row, target_col, depth + 1)
            return "" if out is None else out

        if fname == "HLOOKUP":
            if len(args) < 3:
                return "#VALUE!"
            lookup_val = _sc(args[0])
            if is_excel_error_value(lookup_val):
                return str(lookup_val).upper()
            if lookup_val is None or lookup_val == "":
                return "#N/A"
            tbl_rng = _nd(args[1])
            if not isinstance(tbl_rng, _RangeValue):
                return "#REF!" if tbl_rng == "#REF!" else "#N/A"
            row_num_raw = _to_number(_sc(args[2]))
            if row_num_raw is None or int(row_num_raw) < 1:
                return "#VALUE!"
            sh, r1, c1, r2, c2 = self._bound_range(tbl_rng)
            target_row = r1 + int(row_num_raw) - 1
            if target_row > r2:
                return "#REF!"
            nk = _norm_lookup_key(lookup_val)
            for c in range(c1, c2 + 1):
                cv = self.get_cell_value(sh, r1, c, depth + 1)
                if _norm_lookup_key(cv) == nk:
                    out = self.get_cell_value(sh, target_row, c, depth + 1)
                    return "" if out is None else out
            return "#N/A"

        if fname == "XLOOKUP":
            if len(args) < 3:
                return "#VALUE!"
            lookup_val = _sc(args[0])
            if is_excel_error_value(lookup_val):
                return str(lookup_val).upper()
            look_rng = _nd(args[1])
            ret_rng = _nd(args[2])
            not_found_val: Any = "#N/A"
            if len(args) >= 4 and args[3] != ("lit", None):
                not_found_val = _sc(args[3])
            if lookup_val is None or lookup_val == "":
                return not_found_val

            nk = _norm_lookup_key(lookup_val)
            if isinstance(look_rng, _ArrayValue):
                vec = self._flatten_1d_array(look_rng)
                if vec is None:
                    return "#VALUE!"
                hit_offset = next(
                    (i for i, item in enumerate(vec) if not is_excel_error_value(item) and _norm_lookup_key(item) == nk),
                    None,
                )
                if hit_offset is None:
                    return not_found_val
                if isinstance(ret_rng, _RangeValue):
                    r_sh, rr1, rc1, rr2, rc2 = self._bound_range(ret_rng)
                    out = (
                        self.get_cell_value(r_sh, rr1 + hit_offset, rc1, depth + 1)
                        if rc1 == rc2
                        else self.get_cell_value(r_sh, rr1, rc1 + hit_offset, depth + 1)
                    )
                    return "" if out is None else out
                if isinstance(ret_rng, _ArrayValue):
                    r_vec = self._flatten_1d_array(ret_rng)
                    if r_vec and hit_offset < len(r_vec):
                        out = r_vec[hit_offset]
                        return "" if out is None else out
                return not_found_val

            if not isinstance(look_rng, _RangeValue) or not isinstance(ret_rng, _RangeValue):
                return "#REF!" if (look_rng == "#REF!" or ret_rng == "#REF!") else not_found_val

            l_sh, lr1, lc1, lr2, lc2 = self._bound_range(look_rng)
            r_sh, rr1, rc1, rr2, rc2 = self._bound_range(ret_rng)
            if lc1 == lc2:
                idx_map = self._get_exact_col_index(l_sh, lr1, lr2, lc1, depth)
                hit_row = idx_map.get(nk)
                if hit_row is None:
                    return not_found_val
                offset = hit_row - lr1
                out = self.get_cell_value(r_sh, rr1 + offset, rc1, depth + 1)
                return "" if out is None else out
            for c in range(lc1, lc2 + 1):
                if _norm_lookup_key(self.get_cell_value(l_sh, lr1, c, depth + 1)) == nk:
                    out = self.get_cell_value(r_sh, rr1, rc1 + (c - lc1), depth + 1)
                    return "" if out is None else out
            return not_found_val

        if fname == "MATCH":
            if len(args) < 2:
                return "#VALUE!"
            lookup_val = _sc(args[0])
            if is_excel_error_value(lookup_val):
                return str(lookup_val).upper()
            if lookup_val is None or lookup_val == "":
                return "#N/A"
            look_rng = _nd(args[1])
            match_type = 1
            if len(args) >= 3 and args[2] != ("lit", None):
                mt_num = _to_number(_sc(args[2]))
                if mt_num is not None:
                    match_type = int(mt_num)
            nk = _norm_lookup_key(lookup_val)

            if isinstance(look_rng, _ArrayValue):
                vec = self._flatten_1d_array(look_rng)
                if vec is None:
                    return "#N/A"
                for idx_0b, item in enumerate(vec):
                    if not is_excel_error_value(item) and _norm_lookup_key(item) == nk:
                        return idx_0b + 1
                return "#N/A"

            if not isinstance(look_rng, _RangeValue):
                return "#REF!" if look_rng == "#REF!" else "#N/A"
            sh, r1, c1, r2, c2 = self._bound_range(look_rng)
            if c1 == c2:
                if match_type == 0:
                    idx_map = self._get_exact_col_index(sh, r1, r2, c1, depth)
                    hit_row = idx_map.get(nk)
                    return (hit_row - r1 + 1) if hit_row is not None else "#N/A"
                for r in range(r1, r2 + 1):
                    if _norm_lookup_key(self.get_cell_value(sh, r, c1, depth + 1)) == nk:
                        return r - r1 + 1
                return "#N/A"
            for c in range(c1, c2 + 1):
                if _norm_lookup_key(self.get_cell_value(sh, r1, c, depth + 1)) == nk:
                    return c - c1 + 1
            return "#N/A"

        if fname == "INDEX":
            if len(args) < 2:
                return "#VALUE!"
            arr_rng = _nd(args[0])
            row_raw = _sc(args[1])
            if is_excel_error_value(row_raw):
                return str(row_raw).upper()
            row_num = _to_number(row_raw)
            if row_num is None or int(row_num) < 0:
                return "#VALUE!"
            col_num = 1
            if len(args) >= 3 and args[2] != ("lit", None):
                col_raw = _sc(args[2])
                if is_excel_error_value(col_raw):
                    return str(col_raw).upper()
                cn = _to_number(col_raw)
                if cn is None or int(cn) < 0:
                    return "#VALUE!"
                col_num = max(1, int(cn))
            if isinstance(arr_rng, _ArrayValue):
                ri = max(0, int(row_num) - 1)
                ci = max(0, col_num - 1)
                if ri >= arr_rng.height or ci >= arr_rng.width:
                    return "#REF!"
                out = arr_rng.rows[ri][ci]
                return "" if out is None else out
            if not isinstance(arr_rng, _RangeValue):
                return arr_rng
            sh, r1, c1, r2, c2 = self._bound_range(arr_rng)
            eff_r = r1 + max(0, int(row_num) - 1)
            eff_c = c1 + max(0, col_num - 1)
            if eff_r > max(r2, arr_rng.r2) or eff_c > c2:
                return "#REF!"
            out = self.get_cell_value(sh, eff_r, eff_c, depth + 1)
            return "" if out is None else out

        if fname == "LOOKUP":
            if len(args) < 2:
                return "#VALUE!"
            lookup_val = _sc(args[0])
            if is_excel_error_value(lookup_val):
                return str(lookup_val).upper()
            if lookup_val is None or lookup_val == "":
                return "#N/A"

            look_raw = _nd(args[1])
            look_arr = self._materialize_array(look_raw, depth)
            if look_arr is None:
                return "#REF!" if look_raw == "#REF!" else "#N/A"

            if len(args) >= 3 and args[2] != ("lit", None):
                res_raw = _nd(args[2])
                res_arr = self._materialize_array(res_raw, depth)
                if res_arr is None:
                    return "#REF!" if res_raw == "#REF!" else "#N/A"
                look_vec = self._flatten_1d_array(look_arr)
                res_vec = self._flatten_1d_array(res_arr)
                if look_vec is None or res_vec is None:
                    return "#N/A"
            else:
                if look_arr.width >= look_arr.height and look_arr.height >= 2:
                    look_vec = list(look_arr.rows[0])
                    res_vec = list(look_arr.rows[-1])
                elif look_arr.width >= 2:
                    look_vec = [r[0] for r in look_arr.rows]
                    res_vec = [r[-1] for r in look_arr.rows]
                else:
                    look_vec = self._flatten_1d_array(look_arr) or []
                    res_vec = look_vec

            lv_is_bool = isinstance(lookup_val, bool)
            lv_is_num = isinstance(lookup_val, (int, float)) and not lv_is_bool
            lv_is_str = isinstance(lookup_val, str)

            hit_idx: Optional[int] = None
            for i_pos, item in enumerate(look_vec):
                if item is None or item == "" or is_excel_error_value(item):
                    continue
                it_is_bool = isinstance(item, bool)
                it_is_num = isinstance(item, (int, float)) and not it_is_bool
                it_is_str = isinstance(item, str)
                if (lv_is_bool != it_is_bool) or (lv_is_num != it_is_num) or (lv_is_str != it_is_str):
                    continue
                if _compare_excel_values(item, lookup_val, "<="):
                    hit_idx = i_pos
            if hit_idx is None or hit_idx >= len(res_vec):
                return "#N/A"
            out = res_vec[hit_idx]
            return "" if out is None else out

        if fname == "CHOOSE":
            if len(args) < 2:
                return "#VALUE!"
            idx_raw = _nd(args[0])
            if isinstance(idx_raw, (_RangeValue, _ArrayValue)):
                idx_arr = self._materialize_array(idx_raw, depth)
                idx_vec = self._flatten_1d_array(idx_arr) if idx_arr else None
                if not idx_vec:
                    return "#VALUE!"
                col_arrays: List[List[Any]] = []
                max_h = 1
                for raw_k in idx_vec:
                    kn = _to_number(raw_k)
                    if kn is None or int(kn) < 1 or int(kn) >= len(args):
                        return "#VALUE!"
                    chosen = _nd(args[int(kn)])
                    c_arr = self._materialize_array(chosen, depth)
                    if c_arr is not None:
                        flat_c = self._flatten_1d_array(c_arr)
                        vec_c = flat_c if flat_c is not None else [r[0] for r in c_arr.rows]
                    else:
                        vec_c = [chosen]
                    max_h = max(max_h, len(vec_c))
                    col_arrays.append(vec_c)
                if max_h * len(col_arrays) > _MAX_ARRAY_CELLS:
                    return "#VALUE!"
                merged_rows = [
                    [
                        col_v[r_i] if r_i < len(col_v) else (col_v[0] if len(col_v) == 1 else "#N/A")
                        for col_v in col_arrays
                    ]
                    for r_i in range(max_h)
                ]
                return _ArrayValue(merged_rows, 1, 1)

            idx_sc = idx_raw
            if is_excel_error_value(idx_sc):
                return str(idx_sc).upper()
            kn = _to_number(idx_sc)
            if kn is None or int(kn) < 1 or int(kn) >= len(args):
                return "#VALUE!"
            return _nd(args[int(kn)])

        if fname == "INDIRECT":
            if not args or depth > 64:
                return "#REF!"
            ref_val = _sc(args[0])
            if is_excel_error_value(ref_val):
                return str(ref_val).upper()
            ref_str = _to_excel_str(ref_val).strip()
            if not ref_str or ("[" in ref_str and "]" in ref_str and "!" in ref_str):
                return "#REF!"
            try:
                ref_ast = self._parse_expr_ast(ref_str)
            except Exception:
                return "#REF!"
            if ref_ast[0] not in ("cell", "range", "name", "sref"):
                return "#REF!"
            return self._eval_node(ref_ast, current_sheet, depth + 1, cur_row, cur_col)

        if fname == "OFFSET":
            if len(args) < 3:
                return "#VALUE!"
            base_node = args[0]
            if base_node[0] == "cell":
                _, sh_opt, b_r1, b_c1 = base_node
                b_sh = self._resolve_sheet_title(sh_opt, current_sheet)
                if not b_sh:
                    return "#REF!"
                b_r2, b_c2 = b_r1, b_c1
            else:
                base_val = _nd(base_node)
                if not isinstance(base_val, _RangeValue):
                    return "#REF!" if base_val == "#REF!" else "#VALUE!"
                b_sh, b_r1, b_c1, b_r2, b_c2 = (
                    base_val.sheet,
                    base_val.r1,
                    base_val.c1,
                    base_val.r2,
                    base_val.c2,
                )
            dr_num = _to_number(_sc(args[1]))
            dc_num = _to_number(_sc(args[2]))
            if dr_num is None or dc_num is None:
                return "#VALUE!"
            h_num = (b_r2 - b_r1 + 1)
            w_num = (b_c2 - b_c1 + 1)
            if len(args) >= 4 and args[3] != ("lit", None):
                hn = _to_number(_sc(args[3]))
                if hn is None or int(hn) < 1:
                    return "#REF!" if hn is not None and int(hn) < 1 else "#VALUE!"
                h_num = int(hn)
            if len(args) >= 5 and args[4] != ("lit", None):
                wn = _to_number(_sc(args[4]))
                if wn is None or int(wn) < 1:
                    return "#REF!" if wn is not None and int(wn) < 1 else "#VALUE!"
                w_num = int(wn)
            nr1 = b_r1 + int(dr_num)
            nc1 = b_c1 + int(dc_num)
            if nr1 < 1 or nc1 < 1:
                return "#REF!"
            if h_num == 1 and w_num == 1:
                out = self.get_cell_value(b_sh, nr1, nc1, depth + 1)
                return "" if out is None else out
            return _RangeValue(b_sh, nr1, nc1, nr1 + h_num - 1, nc1 + w_num - 1, False)

        # 3. 文本处理函数
        if fname in ("CONCAT", "CONCATENATE"):
            parts: List[str] = []
            for v in _sc_iter(args):
                if is_excel_error_value(v):
                    return str(v).upper()
                parts.append(_to_excel_str(v))
            return "".join(parts)

        if fname == "TEXTJOIN":
            if len(args) < 3:
                return "#VALUE!"
            delim = _to_excel_str(_sc(args[0]))
            ignore_empty = _is_truthy_excel(_sc(args[1]))
            parts = []
            for v in _sc_iter(args[2:]):
                if is_excel_error_value(v):
                    return str(v).upper()
                sv = _to_excel_str(v)
                if ignore_empty and sv == "":
                    continue
                parts.append(sv)
            return delim.join(parts)

        if fname in ("TRIM", "CLEAN", "UPPER", "LOWER", "PROPER", "LEN", "VALUE"):
            if not args:
                return "#VALUE!"
            v = _sc(args[0])
            if is_excel_error_value(v):
                return str(v).upper()
            sv = _to_excel_str(v)
            if fname == "TRIM":
                return " ".join(sv.split())
            if fname == "CLEAN":
                return "".join(ch for ch in sv if ord(ch) >= 32)
            if fname == "UPPER":
                return sv.upper()
            if fname == "LOWER":
                return sv.lower()
            if fname == "PROPER":
                return sv.title()
            if fname == "LEN":
                return len(sv)
            if fname == "VALUE":
                num = _to_number(sv)
                if num is None:
                    return "#VALUE!"
                return int(num) if float(num).is_integer() else num

        if fname in ("LEFT", "RIGHT"):
            if not args:
                return "#VALUE!"
            v = _sc(args[0])
            if is_excel_error_value(v):
                return str(v).upper()
            sv = _to_excel_str(v)
            cnt = 1
            if len(args) >= 2 and args[1] != ("lit", None):
                n_raw = _to_number(_sc(args[1]))
                if n_raw is None or int(n_raw) < 0:
                    return "#VALUE!"
                cnt = int(n_raw)
            if cnt == 0:
                return ""
            return sv[:cnt] if fname == "LEFT" else sv[-cnt:]

        if fname == "MID":
            if len(args) < 3:
                return "#VALUE!"
            v = _sc(args[0])
            if is_excel_error_value(v):
                return str(v).upper()
            sv = _to_excel_str(v)
            st = _to_number(_sc(args[1]))
            ln = _to_number(_sc(args[2]))
            if st is None or ln is None or int(st) < 1 or int(ln) < 0:
                return "#VALUE!"
            s_i = int(st) - 1
            return sv[s_i : s_i + int(ln)]

        if fname == "SUBSTITUTE":
            if len(args) < 3:
                return "#VALUE!"
            sv = _to_excel_str(_sc(args[0]))
            old_s = _to_excel_str(_sc(args[1]))
            new_s = _to_excel_str(_sc(args[2]))
            if not old_s:
                return sv
            if len(args) >= 4 and args[3] != ("lit", None):
                inst = _to_number(_sc(args[3]))
                if inst is None or int(inst) < 1:
                    return "#VALUE!"
                target_n = int(inst)
                idx = -1
                for _ in range(target_n):
                    idx = sv.find(old_s, idx + 1)
                    if idx == -1:
                        return sv
                return sv[:idx] + new_s + sv[idx + len(old_s) :]
            return sv.replace(old_s, new_s)

        if fname == "REPLACE":
            if len(args) < 4:
                return "#VALUE!"
            sv = _to_excel_str(_sc(args[0]))
            st = _to_number(_sc(args[1]))
            cnt = _to_number(_sc(args[2]))
            new_s = _to_excel_str(_sc(args[3]))
            if st is None or cnt is None or int(st) < 1 or int(cnt) < 0:
                return "#VALUE!"
            s_i = int(st) - 1
            return sv[:s_i] + new_s + sv[s_i + int(cnt) :]

        if fname in ("FIND", "SEARCH"):
            if len(args) < 2:
                return "#VALUE!"
            sub_s = _to_excel_str(_sc(args[0]))
            in_s = _to_excel_str(_sc(args[1]))
            start_1b = 1
            if len(args) >= 3 and args[2] != ("lit", None):
                sn = _to_number(_sc(args[2]))
                if sn is None or int(sn) < 1:
                    return "#VALUE!"
                start_1b = int(sn)
            hay = in_s if fname == "FIND" else in_s.lower()
            ndl = sub_s if fname == "FIND" else sub_s.lower()
            pos = hay.find(ndl, start_1b - 1)
            return (pos + 1) if pos != -1 else "#VALUE!"

        if fname == "TEXT":
            if not args:
                return ""
            v = _sc(args[0])
            if is_excel_error_value(v):
                return str(v).upper()
            fmt = _to_excel_str(_sc(args[1])) if len(args) > 1 else ""
            if (
                fmt
                and re.fullmatch(r"0+", fmt)
                and isinstance(v, (int, float))
                and not isinstance(v, bool)
            ):
                return str(int(round(v))).zfill(len(fmt))
            return _to_excel_str(v)

        if fname == "EXACT":
            if len(args) < 2:
                return False
            return _to_excel_str(_sc(args[0])) == _to_excel_str(_sc(args[1]))

        if fname == "REPT":
            if len(args) < 2:
                return ""
            sv = _to_excel_str(_sc(args[0]))
            n = _to_number(_sc(args[1]))
            if n is None or int(n) < 0:
                return "#VALUE!"
            return sv * min(int(n), 4096)

        # 4. 常用数值聚合函数
        if fname in ("ROUND", "INT", "ABS"):
            if not args:
                return "#VALUE!"
            num = _to_number(_sc(args[0]))
            if num is None:
                return "#VALUE!"
            if fname == "INT":
                return math.floor(num)
            if fname == "ABS":
                res = abs(num)
                return int(res) if float(res).is_integer() else res
            digits = 0
            if len(args) > 1 and args[1] != ("lit", None):
                dn = _to_number(_sc(args[1]))
                if dn is not None:
                    digits = int(dn)
            res = round(num, digits)
            return int(res) if digits <= 0 or float(res).is_integer() else res

        if fname in ("SUM", "MIN", "MAX", "COUNTA"):
            nums: List[float] = []
            count_non_empty = 0
            for v in _sc_iter(args):
                if is_excel_error_value(v):
                    return str(v).upper()
                if v is not None and v != "":
                    count_non_empty += 1
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    nums.append(float(v))
            if fname == "COUNTA":
                return count_non_empty
            if not nums:
                return 0
            res = sum(nums) if fname == "SUM" else (min(nums) if fname == "MIN" else max(nums))
            return int(res) if float(res).is_integer() else res

        return "#NAME?"


_MERGE_CELL_REF_RE = re.compile(rb'<(?:\w+:)?mergeCell\b[^>]*\bref=["\']([A-Za-z0-9:$]+)["\']')


def _extract_sheet_merged_bounds_from_zip(
    content: bytes, sheet_title: str, sheet_titles: Sequence[str]
) -> List[Tuple[int, int, int, int]]:
    """无需以非只读模式重开整个工作簿，直接从对应 Sheet XML 中毫秒级提取合并单元格坐标."""
    bounds_list: List[Tuple[int, int, int, int]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            xml_to_title = _map_sheet_xml_to_titles(zf)
            target_xml = ""
            for xp, st in xml_to_title.items():
                if st == sheet_title:
                    target_xml = xp
                    break
            if not target_xml and sheet_title in sheet_titles:
                idx_1b = list(sheet_titles).index(sheet_title) + 1
                cand = f"xl/worksheets/sheet{idx_1b}.xml"
                if cand in zf.namelist():
                    target_xml = cand
            if not target_xml or target_xml not in zf.namelist():
                return bounds_list
            raw_xml = zf.read(target_xml)
            if b"mergeCell" not in raw_xml:
                return bounds_list
            for m in _MERGE_CELL_REF_RE.finditer(raw_xml):
                ref_str = m.group(1).decode("ascii", errors="ignore")
                if ":" not in ref_str:
                    continue
                p1, p2 = ref_str.split(":", 1)
                rc1 = _parse_coord(p1)
                rc2 = _parse_coord(p2)
                if rc1 and rc2:
                    min_r, max_r = min(rc1[0], rc2[0]), max(rc1[0], rc2[0])
                    min_c, max_c = min(rc1[1], rc2[1]), max(rc1[1], rc2[1])
                    bounds_list.append((min_c, min_r, max_c, max_r))
    except Exception:
        return bounds_list
    return bounds_list


class ResolvedWorkbook:
    """统一工作簿读取器：优先采用缓存结果，遇到未缓存公式或文本态公式时自动求值."""

    def __init__(self, content: bytes, *, read_only_preferred: bool = True) -> None:
        self._scan = _scan_xlsx_package(content)
        self._content = self._scan.normalized_content or content
        self._has_uncached = self._scan.has_uncached
        self._evaluator: Optional[WorkbookFormulaEvaluator] = None

        if self._has_uncached:
            self._evaluator = WorkbookFormulaEvaluator(
                self._content,
                self._scan.cached_empty_by_title,
                package_scan=self._scan,
            )
            self._wb = self._evaluator._wb_val
            self._is_read_only = True
        else:
            self._wb = openpyxl.load_workbook(
                io.BytesIO(self._content),
                read_only=read_only_preferred,
                data_only=True,
            )
            self._is_read_only = read_only_preferred

        # 在 reset_dimensions() 清空 ReadOnlyWorksheet.max_row 之前缓存 XML <dimension> 与真实 <row> 行数估计值
        self._initial_max_rows: Dict[str, int] = {}
        sheet_lower_map: Dict[str, str] = {}
        for title in self._wb.sheetnames:
            sheet_lower_map[title.lower()] = title
            try:
                self._initial_max_rows[title] = int(getattr(self._wb[title], "max_row", 0) or 0)
            except Exception:
                self._initial_max_rows[title] = 0

        for sh_key, scan_rows in (self._scan.sheet_row_counts or {}).items():
            resolved = sh_key if sh_key in self._initial_max_rows else sheet_lower_map.get(sh_key.strip().lower())
            if resolved:
                self._initial_max_rows[resolved] = max(self._initial_max_rows.get(resolved, 0), int(scan_rows))

        self._cached_empty: Dict[str, Set[Tuple[int, int]]] = {}
        for sh_key, coords in (self._scan.cached_empty_by_title or {}).items():
            resolved = sh_key if sh_key in self._initial_max_rows else sheet_lower_map.get(sh_key.strip().lower())
            if resolved:
                self._cached_empty[resolved] = coords

    def __enter__(self) -> "ResolvedWorkbook":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._evaluator is not None:
            self._evaluator.close()
        if self._wb is not None and (
            self._evaluator is None or self._wb is not self._evaluator._wb_val
        ):
            try:
                self._wb.close()
            except Exception:
                pass

    @property
    def sheetnames(self) -> List[str]:
        return list(self._wb.sheetnames)

    def _ensure_evaluator(self) -> WorkbookFormulaEvaluator:
        if self._evaluator is None:
            self._evaluator = WorkbookFormulaEvaluator(
                self._content,
                self._cached_empty,
                package_scan=self._scan,
            )
        return self._evaluator

    def estimate_sheet_rows(self, sheet_title: str) -> int:
        """返回工作表预估总行数（优先保留 <dimension> 元数据，兼顾稀疏公式最大行号）."""
        est = int(self._initial_max_rows.get(sheet_title, 0) or 0)
        if self._has_uncached and self._evaluator is not None:
            est = max(est, self._evaluator._fml_max_dims.get(sheet_title, (0, 0))[0])
        return est

    def iter_sheet_rows(
        self,
        sheet_title: str,
        *,
        max_rows: Optional[int] = None,
        row_progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> Iterator[List[Any]]:
        """按行流式产出工作表已解析最终结果的单元格列表."""
        if self._has_uncached and self._evaluator is not None:
            ev = self._evaluator
            total_r = ev._sheet_max_row(sheet_title)
            if max_rows is not None:
                total_r = min(total_r, max_rows)
            val_mat = ev._get_val_matrix(sheet_title)
            for r_1b in range(1, total_r + 1):
                width = ev._sheet_row_width(sheet_title, r_1b, val_mat)
                out_row = [ev.get_cell_value(sheet_title, r_1b, c_1b) for c_1b in range(1, width + 1)]
                if row_progress_cb is not None and (r_1b == 1 or r_1b % 50 == 0 or r_1b == total_r):
                    row_progress_cb(r_1b, total_r)
                yield out_row
            return

        ws = self._wb[sheet_title]
        est_max = self.estimate_sheet_rows(sheet_title)
        if max_rows is not None and est_max > 0:
            est_max = min(est_max, max_rows)
        if hasattr(ws, "reset_dimensions"):
            try:
                ws.reset_dimensions()
            except Exception:
                pass

        empty_coords = self._cached_empty.get(sheet_title, ())
        last_r_idx = 0
        last_reported_idx = 0
        for r_idx, raw_row in enumerate(ws.iter_rows(values_only=True), start=1):
            if max_rows is not None and r_idx > max_rows:
                break
            last_r_idx = r_idx
            row_list = list(raw_row) if raw_row else []
            for c_idx, val in enumerate(row_list):
                if val is None and empty_coords and (r_idx, c_idx + 1) in empty_coords:
                    row_list[c_idx] = ""
                elif isinstance(val, str) and _TEXT_FORMULA_RE.match(val.strip()):
                    row_list[c_idx] = self._ensure_evaluator().evaluate_formula_string(
                        val.strip(), sheet_title, cur_row=r_idx, cur_col=c_idx + 1
                    )
            if row_progress_cb is not None and (
                r_idx == 1 or r_idx % 100 == 0 or (est_max > 0 and r_idx == est_max)
            ):
                last_reported_idx = r_idx
                row_progress_cb(r_idx, max(est_max, r_idx))
            yield row_list

        if row_progress_cb is not None and last_r_idx > 0 and last_reported_idx != last_r_idx:
            row_progress_cb(last_r_idx, max(est_max, last_r_idx))

    def extract_sheet_rows_with_merged(
        self,
        sheet_title: str,
        *,
        max_rows: Optional[int] = None,
        row_progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> List[List[Any]]:
        """提取工作表行（解析公式结果并展开合并单元格向下/向右填充）."""
        matrix = [
            list(r)
            for r in self.iter_sheet_rows(
                sheet_title,
                max_rows=max_rows,
                row_progress_cb=row_progress_cb,
            )
        ]
        if not matrix:
            return matrix
        if self._is_read_only:
            bounds = _extract_sheet_merged_bounds_from_zip(
                self._content, sheet_title, self.sheetnames
            )
            self._fill_merged_bounds(bounds, matrix)
            return matrix

        ws = self._wb[sheet_title]
        bounds = [mr.bounds for mr in ws.merged_cells.ranges]
        self._fill_merged_bounds(bounds, matrix)
        return matrix

    @staticmethod
    def _fill_merged_bounds(
        bounds_list: Sequence[Tuple[int, int, int, int]], matrix: List[List[Any]]
    ) -> None:
        if not matrix or not bounds_list:
            return
        try:
            for min_col, min_row, max_col, max_row in bounds_list:
                if min_row - 1 < len(matrix) and min_col - 1 < len(matrix[min_row - 1]):
                    top_left_val = matrix[min_row - 1][min_col - 1]
                else:
                    top_left_val = None
                if top_left_val is None or top_left_val == "":
                    continue
                for r_idx in range(min_row - 1, min(max_row, len(matrix))):
                    row_list = matrix[r_idx]
                    if len(row_list) < max_col:
                        row_list.extend([None] * (max_col - len(row_list)))
                    for c_idx in range(min_col - 1, min(max_col, len(row_list))):
                        if row_list[c_idx] is None or row_list[c_idx] == "":
                            row_list[c_idx] = top_left_val
        except Exception:
            pass
