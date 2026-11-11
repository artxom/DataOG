"""Excel 贯标清单解析、安全校验、模板生成与结果报告导出模块.

安全特性：
- 启用 defusedxml 防止 XLSX (OOXML) 解析过程中的 XXE (XML External Entity) 攻击
- 严格校验文件大小上限 (10MB)、文件头魔数 (PK\\x03\\x04) 及 ZIP 内文件路径防穿越
- 支持多 Sheet 自动扫描、合并单元格自动展开、全角符号清洗与智能表头模糊识别
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

try:
    import defusedxml  # type: ignore

    defusedxml.defuse_stdlib()
except ImportError:
    pass

MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
XLSX_MAGIC_BYTES = b"PK\x03\x04"
FORBIDDEN_XML_PATTERNS = re.compile(
    rb"<!DOCTYPE\s+|<!ENTITY\s+|http://www\.w3\.org/2001/XInclude",
    re.IGNORECASE,
)

# 表头同义词映射表（忽略大小写、空格及括号差异）
HEADER_SYNONYMS: Dict[str, List[str]] = {
    "schema_name": [
        "模式名",
        "模式",
        "模式名称",
        "schema",
        "schema_name",
        "schemaname",
        "schema名称",
        "所属schema",
        "目标schema",
        "源schema",
        "库名",
        "数据库名",
        "数据库模式",
        "所属模式",
        "所属库",
        "owner",
        "db_schema",
    ],
    "table_name": [
        "表名",
        "表名称",
        "表英文名",
        "表英文名称",
        "物理表名",
        "英文表名",
        "数据表",
        "数据表名",
        "目标表",
        "目标表名",
        "源表",
        "源表名",
        "表代码",
        "数据库表名",
        "table",
        "table_name",
        "tablename",
        "tbl_name",
        "table_code",
    ],
    "old_col_name": [
        "修改前字段名",
        "修改前名称",
        "修改前字段名称",
        "修改前字段",
        "修改前字段英文名",
        "修改前英文名",
        "改造前字段名",
        "改造前字段",
        "改造前字段英文名",
        "贯标前字段名",
        "贯标前字段",
        "贯标前英文名",
        "贯标前字段英文名",
        "原字段名",
        "原字段",
        "原字段名称",
        "原字段英文名",
        "原英文名",
        "旧字段名",
        "旧字段",
        "旧字段名称",
        "旧字段英文名",
        "现字段名",
        "当前字段名",
        "字段名(修改前)",
        "字段名（修改前）",
        "字段英文名(原)",
        "字段英文名（原）",
        "源字段名",
        "源字段",
        "源字段英文名",
        "old_col_name",
        "old_column",
        "old_column_name",
        "old_name",
        "src_col_name",
        "source_column",
    ],
    "old_col_type": [
        "修改前类型",
        "修改前字段类型",
        "修改前数据类型",
        "改造前类型",
        "改造前字段类型",
        "改造前数据类型",
        "贯标前类型",
        "贯标前字段类型",
        "贯标前数据类型",
        "原字段类型",
        "原类型",
        "原数据类型",
        "旧字段类型",
        "旧类型",
        "旧数据类型",
        "现字段类型",
        "当前类型",
        "类型(修改前)",
        "类型（修改前）",
        "数据类型(原)",
        "数据类型（原）",
        "源字段类型",
        "源数据类型",
        "old_col_type",
        "old_type",
        "old_data_type",
        "src_col_type",
    ],
    "old_col_len": [
        "修改前长度",
        "修改前字段长度",
        "原字段长度",
        "原长度",
        "旧长度",
        "改造前长度",
        "贯标前长度",
        "old_length",
        "old_len",
    ],
    "old_col_comment": [
        "修改前注释",
        "修改前字段注释",
        "修改前中文名",
        "修改前字段中文名",
        "修改前说明",
        "改造前注释",
        "改造前中文名",
        "贯标前注释",
        "贯标前中文名",
        "原字段注释",
        "原注释",
        "原字段中文名",
        "原中文名",
        "原中文名称",
        "原字段说明",
        "旧字段注释",
        "旧注释",
        "旧中文名",
        "注释(修改前)",
        "注释（修改前）",
        "old_col_comment",
        "old_comment",
        "src_comment",
    ],
    "new_col_name": [
        "修改后字段名",
        "修改后名称",
        "修改后字段名称",
        "修改后字段",
        "修改后字段英文名",
        "修改后英文名",
        "改造后字段名",
        "改造后字段",
        "改造后字段英文名",
        "贯标后字段名",
        "贯标后字段",
        "贯标后英文名",
        "贯标后字段英文名",
        "标准字段名",
        "标准字段英文名",
        "标准英文名",
        "新字段名",
        "新字段",
        "新字段名称",
        "新字段英文名",
        "新英文名",
        "目标字段名",
        "目标字段",
        "目标字段英文名",
        "字段名(修改后)",
        "字段名（修改后）",
        "字段英文名(新)",
        "字段英文名（新）",
        "new_col_name",
        "new_column",
        "new_column_name",
        "new_name",
        "target_col_name",
        "target_column",
        "std_col_name",
    ],
    "new_col_type": [
        "修改后类型",
        "修改后字段类型",
        "修改后数据类型",
        "改造后类型",
        "改造后字段类型",
        "改造后数据类型",
        "贯标后类型",
        "贯标后字段类型",
        "贯标后数据类型",
        "标准类型",
        "标准字段类型",
        "标准数据类型",
        "新字段类型",
        "新类型",
        "新数据类型",
        "目标类型",
        "目标字段类型",
        "目标数据类型",
        "类型(修改后)",
        "类型（修改后）",
        "数据类型(新)",
        "数据类型（新）",
        "new_col_type",
        "new_type",
        "new_data_type",
        "target_col_type",
        "std_col_type",
    ],
    "new_col_len": [
        "修改后长度",
        "修改后字段长度",
        "新字段长度",
        "新长度",
        "目标长度",
        "标准长度",
        "改造后长度",
        "贯标后长度",
        "new_length",
        "new_len",
    ],
    "new_col_comment": [
        "修改后注释",
        "修改后字段注释",
        "修改后中文名",
        "修改后字段中文名",
        "修改后说明",
        "改造后注释",
        "改造后中文名",
        "贯标后注释",
        "贯标后中文名",
        "标准注释",
        "标准中文名",
        "标准字段中文名",
        "新字段注释",
        "新注释",
        "新中文名",
        "新字段中文名",
        "目标注释",
        "目标中文名",
        "目标字段注释",
        "注释(修改后)",
        "注释（修改后）",
        "new_col_comment",
        "new_comment",
        "target_comment",
    ],
}

# 当 Excel 中仅有一套通用列名（如直接导出的单表结构字典）时的回退同义词
GENERIC_COL_SYNONYMS: Dict[str, List[str]] = {
    "generic_col_name": [
        "字段名",
        "字段名称",
        "字段英文名",
        "英文名",
        "英文名称",
        "列名",
        "列名称",
        "字段代码",
        "column",
        "column_name",
        "columnname",
        "col_name",
        "field",
        "field_name",
    ],
    "generic_col_type": [
        "字段类型",
        "数据类型",
        "类型",
        "data_type",
        "datatype",
        "col_type",
        "column_type",
        "type",
    ],
    "generic_col_comment": [
        "字段注释",
        "注释",
        "字段中文名",
        "中文名",
        "中文名称",
        "字段说明",
        "说明",
        "备注",
        "描述",
        "comment",
        "comments",
        "description",
    ],
}

STATUS_LABELS: Dict[str, str] = {
    "COMPLETED": "已贯标 (与数据库一致)",
    "PENDING": "待贯标",
    "PARTIAL": "部分完成 (需继续更新类型/注释)",
    "FAILED": "执行报错",
    "MISSING_TABLE": "目标表不存在",
    "MISSING_COLUMN": "字段不存在",
    "UNCHECKED": "未核验",
}


def validate_xlsx_bytes(filename: str, content: bytes) -> None:
    """基本检查上传文件内容非空."""
    if not content:
        raise ValueError("上传的 Excel 文件内容为空")


def _normalize_header(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip().lower()
    # 统一全角括号与常见空白分隔符
    text = text.replace("（", "(").replace("）", ")")
    for ch in (" ", "\t", "\n", "\r", "_", "-", "\u200b", "\u3000", ":", "：", "*"):
        text = text.replace(ch, "")
    return text


_IDENT_QUOTE_CHARS = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'


def _clean_ident_value(raw: str) -> str:
    """清理从 Excel 单元格读取的表名、模式名或字段名（去除零宽字符、全角空格、中英文单双引号及尾随括号中文说明）."""
    if not raw:
        return ""
    s = (
        raw.replace("\u200b", "")
        .replace("\u3000", " ")
        .replace("\ufeff", "")
        .strip(_IDENT_QUOTE_CHARS)
    )
    # 如果表名/字段名写成 "dwd_cust_info(客户信息表)" 或 "dwd_cust_info（客户表）"，提取括号前的英文标识符
    m = re.match(r"^([^\s(（]+)\s*[(（].*[)）]$", s)
    if m:
        s = m.group(1).strip(_IDENT_QUOTE_CHARS)
    return s.strip(_IDENT_QUOTE_CHARS)


def _clean_type_value(raw_type: str, raw_len: str = "") -> str:
    """清理从 Excel 读取的数据类型字符串（转换全角括号/逗号，合并独立的长度/精度列）."""
    t = (raw_type or "").replace("\u200b", "").replace("\u3000", " ").strip()
    t = t.replace("（", "(").replace("）", ")").replace("，", ",")
    t = " ".join(t.split())
    if not t:
        return ""

    len_str = (raw_len or "").strip().replace("（", "").replace("）", "").replace("(", "").replace(")", "").replace("，", ",")
    if len_str and "(" not in t:
        # 处理类似 100.0 (Excel 数字格式) -> 100
        if re.match(r"^\d+\.0$", len_str):
            len_str = len_str[:-2]
        if re.match(r"^\d+(\s*,\s*\d+)?$", len_str):
            base_lower = t.lower()
            if base_lower in (
                "varchar",
                "varchar2",
                "nvarchar2",
                "character varying",
                "char",
                "character",
                "bpchar",
                "numeric",
                "decimal",
                "number",
            ):
                t = f"{t}({len_str})"
    return t


def _fuzzy_match_field(cell_text: str) -> Optional[str]:
    """当精确表头同义词未命中时，基于前缀/核心词组进行智能模糊识别."""
    if not cell_text:
        return None
    is_old = any(k in cell_text for k in ("修改前", "改造前", "贯标前", "原", "旧", "源", "old", "src", "before"))
    is_new = any(k in cell_text for k in ("修改后", "改造后", "贯标后", "新", "目标", "标准", "new", "target", "std", "after"))

    has_col_name = any(k in cell_text for k in ("字段名", "字段英文", "英文名", "列名", "字段代码", "colname", "column", "field"))
    has_type = any(k in cell_text for k in ("类型", "type", "datatype"))
    has_len = any(k in cell_text for k in ("长度", "精度", "length", "len"))
    has_comment = any(k in cell_text for k in ("注释", "中文名", "说明", "描述", "备注", "comment", "desc"))

    if is_old:
        if has_type:
            return "old_col_type"
        if has_len:
            return "old_col_len"
        if has_comment:
            return "old_col_comment"
        if has_col_name or cell_text in ("修改前字段", "原字段", "旧字段", "改造前字段", "贯标前字段"):
            return "old_col_name"
    elif is_new:
        if has_type:
            return "new_col_type"
        if has_len:
            return "new_col_len"
        if has_comment:
            return "new_col_comment"
        if has_col_name or cell_text in ("修改后字段", "新字段", "目标字段", "标准字段", "改造后字段", "贯标后字段"):
            return "new_col_name"

    if any(k in cell_text for k in ("表英文", "物理表", "数据表", "表名", "tablename", "tblname")):
        if "中文" not in cell_text and "注释" not in cell_text and "说明" not in cell_text:
            return "table_name"
    if any(k in cell_text for k in ("模式名", "数据库模式", "所属模式", "schemaname", "所属库")):
        return "schema_name"

    return None


def _match_headers(header_row: Sequence[Any]) -> Dict[str, int]:
    """根据表头行匹配各业务字段对应的列索引 (0-based)."""
    mapping: Dict[str, int] = {}
    normalized_cells = [_normalize_header(cell) for cell in header_row]

    # 1. 同义词精确匹配
    for field_key, synonyms in HEADER_SYNONYMS.items():
        norm_synonyms = {_normalize_header(s) for s in synonyms}
        for idx, cell_text in enumerate(normalized_cells):
            if not cell_text or idx in mapping.values():
                continue
            if cell_text in norm_synonyms:
                mapping[field_key] = idx
                break

    # 2. 关键词模糊匹配补充
    for idx, cell_text in enumerate(normalized_cells):
        if not cell_text or idx in mapping.values():
            continue
        guessed = _fuzzy_match_field(cell_text)
        if guessed and guessed not in mapping:
            mapping[guessed] = idx

    # 3. 如果出现了两次完全相同的「字段名 / 类型 / 注释」（左旧右新结构），自动按先后顺序分配给 old 和 new
    if "old_col_name" not in mapping or "new_col_name" not in mapping:
        generic_name_norms = {_normalize_header(s) for s in GENERIC_COL_SYNONYMS["generic_col_name"]}
        generic_type_norms = {_normalize_header(s) for s in GENERIC_COL_SYNONYMS["generic_col_type"]}
        generic_comm_norms = {_normalize_header(s) for s in GENERIC_COL_SYNONYMS["generic_col_comment"]}

        name_cols = [i for i, c in enumerate(normalized_cells) if c in generic_name_norms and i not in mapping.values()]
        type_cols = [i for i, c in enumerate(normalized_cells) if c in generic_type_norms and i not in mapping.values()]
        comm_cols = [i for i, c in enumerate(normalized_cells) if c in generic_comm_norms and i not in mapping.values()]

        if len(name_cols) >= 2:
            mapping.setdefault("old_col_name", name_cols[0])
            mapping.setdefault("new_col_name", name_cols[1])
        elif len(name_cols) == 1:
            if "old_col_name" not in mapping and "new_col_name" in mapping:
                mapping["old_col_name"] = name_cols[0]
            elif "old_col_name" in mapping and "new_col_name" not in mapping:
                mapping["new_col_name"] = name_cols[0]
            else:
                # 只有一列字段名（如数据字典），同时映射为原字段名与目标字段名
                mapping["old_col_name"] = name_cols[0]
                mapping["new_col_name"] = name_cols[0]

        if len(type_cols) >= 2:
            mapping.setdefault("old_col_type", type_cols[0])
            mapping.setdefault("new_col_type", type_cols[1])
        elif len(type_cols) == 1:
            if "old_col_type" not in mapping:
                mapping["old_col_type"] = type_cols[0]
            if "new_col_type" not in mapping:
                mapping["new_col_type"] = type_cols[0]

        if len(comm_cols) >= 2:
            mapping.setdefault("old_col_comment", comm_cols[0])
            mapping.setdefault("new_col_comment", comm_cols[1])
        elif len(comm_cols) == 1:
            if "old_col_comment" not in mapping:
                mapping["old_col_comment"] = comm_cols[0]
            if "new_col_comment" not in mapping:
                mapping["new_col_comment"] = comm_cols[0]

    return mapping


def _extract_sheet_rows_with_merged_cells(sheet: Any) -> List[List[Any]]:
    """读取工作表所有行，并将合并单元格的值自动向下/向右填充."""
    matrix: List[List[Any]] = [list(r) for r in sheet.iter_rows(values_only=True)]
    if not matrix:
        return []

    try:
        for merged_range in sheet.merged_cells.ranges:
            min_col, min_row, max_col, max_row = merged_range.bounds
            if min_row - 1 < len(matrix) and min_col - 1 < len(matrix[min_row - 1]):
                top_left_val = matrix[min_row - 1][min_col - 1]
            else:
                top_left_val = None
            if top_left_val is None:
                continue
            for r_idx in range(min_row - 1, min(max_row, len(matrix))):
                row_list = matrix[r_idx]
                for c_idx in range(min_col - 1, min(max_col, len(row_list))):
                    if row_list[c_idx] is None:
                        row_list[c_idx] = top_left_val
    except Exception:
        pass

    return matrix


def _guess_table_from_sheet_title(title: str) -> str:
    """若工作表内无表名列，且 Sheet 名称本身是合法的英文表名（非 Sheet1 等），提取作为默认表名."""
    cleaned = _clean_ident_value(title or "")
    if not cleaned:
        return ""
    lower = cleaned.lower()
    if lower.startswith("sheet") or lower in ("目录", "封面", "说明", "汇总", "贯标清单", "字段清单", "template"):
        return ""
    if re.match(r"^[a-zA-Z_][a-zA-Z0-9_.$]*$", cleaned):
        return cleaned
    return ""


def parse_excel_checklist(
    content: bytes,
    filename: str = "checklist.xlsx",
    default_schema: str = "public",
    default_table: str = "",
) -> List[Dict[str, Any]]:
    """解析上传的 Excel 贯标清单（支持多 Sheet 扫描、合并单元格展开、表头智能评分），返回标准化列表."""
    validate_xlsx_bytes(filename, content)

    # 不使用 read_only=True，避免第三方导出的 .xlsx 因 <dimension> 元数据缺失只读出第 1 列
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    try:
        if not wb.worksheets:
            raise ValueError("Excel 文件中未找到有效的工作表 (Sheet)")

        parsed_items: List[Dict[str, Any]] = []
        clean_default_schema = _clean_ident_value(default_schema) or "public"
        clean_default_table = _clean_ident_value(default_table)

        for sheet in wb.worksheets:
            rows = _extract_sheet_rows_with_merged_cells(sheet)
            if not rows:
                continue

            # 在前 30 行内评分寻找最佳表头行
            best_header_idx = -1
            best_header_map: Dict[str, int] = {}
            best_score = 0

            for idx, row in enumerate(rows[:30]):
                non_empty = [c for c in row if c is not None and str(c).strip() != ""]
                if len(non_empty) < 2:
                    continue
                candidate_map = _match_headers(row)
                score = len(candidate_map)
                if "old_col_name" in candidate_map:
                    score += 3
                if "new_col_name" in candidate_map:
                    score += 3
                if "table_name" in candidate_map:
                    score += 2
                if score > best_score and ("old_col_name" in candidate_map or "new_col_name" in candidate_map):
                    best_score = score
                    best_header_idx = idx
                    best_header_map = candidate_map

            # 若该 Sheet 未通过名称匹配到表头，但工作簿只有 1 个 Sheet 且第 1 个非空行 >= 6 列，提供按列位置兜底
            if best_header_idx == -1 and len(wb.worksheets) == 1:
                for idx, row in enumerate(rows[:10]):
                    non_empty_indices = [
                        i for i, c in enumerate(row) if c is not None and str(c).strip() != ""
                    ]
                    if len(non_empty_indices) >= 6:
                        best_header_idx = idx
                        if len(non_empty_indices) >= 8:
                            keys_order = [
                                "schema_name",
                                "table_name",
                                "old_col_name",
                                "old_col_type",
                                "old_col_comment",
                                "new_col_name",
                                "new_col_type",
                                "new_col_comment",
                            ]
                        elif len(non_empty_indices) == 7:
                            keys_order = [
                                "table_name",
                                "old_col_name",
                                "old_col_type",
                                "old_col_comment",
                                "new_col_name",
                                "new_col_type",
                                "new_col_comment",
                            ]
                        else:
                            keys_order = [
                                "old_col_name",
                                "old_col_type",
                                "old_col_comment",
                                "new_col_name",
                                "new_col_type",
                                "new_col_comment",
                            ]
                        best_header_map = {
                            k: col_i for k, col_i in zip(keys_order, non_empty_indices)
                        }
                        break

            if best_header_idx == -1:
                continue

            header_map = best_header_map
            sheet_default_table = clean_default_table or _guess_table_from_sheet_title(sheet.title)
            last_schema = clean_default_schema
            last_table = sheet_default_table

            for row_num, row in enumerate(
                rows[best_header_idx + 1 :], start=best_header_idx + 2
            ):
                if not row or all(c is None or str(c).strip() == "" for c in row):
                    continue

                def _get_val(key: str) -> str:
                    col_idx = header_map.get(key)
                    if col_idx is None or col_idx >= len(row):
                        return ""
                    val = row[col_idx]
                    return "" if val is None else str(val).strip()

                raw_schema = _clean_ident_value(_get_val("schema_name"))
                raw_table = _clean_ident_value(_get_val("table_name"))

                if raw_schema:
                    last_schema = raw_schema
                if raw_table:
                    last_table = raw_table

                schema_val = last_schema
                table_val = last_table
                if "." in table_val:
                    parts = table_val.split(".", 1)
                    if not raw_schema and parts[0].strip():
                        schema_val = parts[0].strip()
                    table_val = parts[1].strip()

                old_col_name = _clean_ident_value(_get_val("old_col_name"))
                new_col_name = _clean_ident_value(_get_val("new_col_name")) or old_col_name
                if not old_col_name and new_col_name:
                    old_col_name = new_col_name

                # 跳过重复的表头行或合计说明行
                if not old_col_name and not new_col_name:
                    continue
                if _normalize_header(old_col_name) in (
                    "修改前字段名",
                    "原字段名",
                    "旧字段名",
                    "字段名",
                    "oldcolname",
                ):
                    continue

                old_col_type = _clean_type_value(
                    _get_val("old_col_type"), _get_val("old_col_len")
                )
                new_col_type = (
                    _clean_type_value(_get_val("new_col_type"), _get_val("new_col_len"))
                    or old_col_type
                )
                old_col_comment = _get_val("old_col_comment")
                new_col_comment = _get_val("new_col_comment")

                if not table_val:
                    raise ValueError(
                        f"工作表 [{sheet.title}] 第 {row_num} 行缺少「表名」，请在 Excel 中补充「表名」列或在上传弹窗中填写「默认表名」"
                    )

                parsed_items.append(
                    {
                        "row_index": len(parsed_items) + 1,
                        "excel_row": row_num,
                        "schema_name": schema_val or "public",
                        "table_name": table_val,
                        "old_col_name": old_col_name,
                        "old_col_type": old_col_type,
                        "old_col_comment": old_col_comment,
                        "new_col_name": new_col_name,
                        "new_col_type": new_col_type,
                        "new_col_comment": new_col_comment,
                    }
                )

        if not parsed_items:
            raise ValueError(
                "未能从 Excel 中解析到有效的贯标字段行。请确认表头包含「表名、修改前字段名、修改后字段名、修改后类型」等列（可点击下载标准模板对照）。"
            )

        return parsed_items
    finally:
        wb.close()


def generate_template_excel() -> bytes:
    """生成带华为 DWS 标准格式示例的 Excel 贯标模板."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DWS字段贯标清单"

    headers = [
        "模式名",
        "表名",
        "修改前字段名",
        "修改前类型",
        "修改前注释",
        "修改后字段名",
        "修改后类型",
        "修改后注释",
    ]
    ws.append(headers)

    sample_rows = [
        [
            "public",
            "dwd_cust_info",
            "cust_no",
            "varchar(32)",
            "客户号",
            "party_id",
            "varchar(64)",
            "当事人统一编号",
        ],
        [
            "public",
            "dwd_cust_info",
            "cust_nm",
            "varchar(100)",
            "客户姓名",
            "party_name",
            "varchar(200)",
            "当事人名称",
        ],
    ]

    for row in sample_rows:
        ws.append(row)

    _style_worksheet(ws, len(headers))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_report_excel(items: List[Dict[str, Any]]) -> bytes:
    """将当前贯标状态、数据库实际属性、字段顺序 attnum 及报错信息导出为 Excel 报告."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DWS贯标执行结果报告"

    headers = [
        "序号",
        "模式名",
        "表名",
        "表内字段顺序(attnum)",
        "修改前字段名",
        "修改前类型",
        "修改前注释",
        "修改后字段名",
        "修改后类型",
        "修改后注释",
        "贯标状态",
        "执行方式",
        "库内当前字段名",
        "库内当前类型",
        "库内当前注释",
        "状态说明 / 报错详情",
        "执行的SQL语句",
        "最后更新时间",
    ]
    ws.append(headers)

    status_fills = {
        "COMPLETED": PatternFill(start_color="E6F4EA", end_color="E6F4EA", fill_type="solid"),
        "FAILED": PatternFill(start_color="FCE8E6", end_color="FCE8E6", fill_type="solid"),
        "PENDING": PatternFill(start_color="FEF7E0", end_color="FEF7E0", fill_type="solid"),
        "PARTIAL": PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid"),
    }

    for item in items:
        st = item.get("status", "UNCHECKED")
        pos_str = (
            f"第 {item['ordinal_position']} / {item['total_columns']} 列"
            if item.get("ordinal_position")
            else "-"
        )
        strategy_label = (
            "原序重建表迁移"
            if item.get("strategy") == "TABLE_REBUILD"
            else "原生 ALTER COLUMN"
        )
        detail = item.get("error_message") or item.get("status_detail") or ""

        row_vals = [
            item.get("row_index", ""),
            item.get("schema_name", ""),
            item.get("table_name", ""),
            pos_str,
            item.get("old_col_name", ""),
            item.get("old_col_type", ""),
            item.get("old_col_comment", ""),
            item.get("new_col_name", ""),
            item.get("new_col_type", ""),
            item.get("new_col_comment", ""),
            STATUS_LABELS.get(st, st),
            strategy_label,
            item.get("db_current_col_name", ""),
            item.get("db_current_type", ""),
            item.get("db_current_comment", ""),
            detail,
            item.get("generated_sql", ""),
            item.get("updated_at", ""),
        ]
        ws.append(row_vals)
        row_idx = ws.max_row
        fill = status_fills.get(st)
        if fill:
            for col_i in range(1, len(headers) + 1):
                ws.cell(row=row_idx, column=col_i).fill = fill

    _style_worksheet(ws, len(headers))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _style_worksheet(ws: Any, col_count: int) -> None:
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    body_font = Font(name="Microsoft YaHei", size=10)
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    for col_idx in range(1, col_count + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = 20

    ws.row_dimensions[1].height = 26
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=col_count):
        for cell in row:
            cell.font = body_font
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center", wrap_text=False)
