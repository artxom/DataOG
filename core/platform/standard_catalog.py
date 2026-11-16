"""DataOG 全局数据标准字典、多库物理格式解析、字段绑定与落标核验中枢 (StandardCatalog).

核心职责：
1. 统一维护平台级唯一一套最新「标准字典」（以 `数据项编号` `std_code` 为主键），完整兼容 46 列银行/金融行业数据字典 Excel 格式；
2. 采用流式只读解析与单事务批量 Upsert，秒级导入 2.6 万+ 条数据标准并保留既有表字段绑定关系；
3. 根据目标数据库方言 (`db_type`) 与用户选择的物理格式档 (`format_profile`) 智能解析标准目标类型，并提供三级落标核验 (`COMPLIANT` / `COMMENT_ONLY` / `NON_COMPLIANT` / `UNBOUND`)；
4. 按数据源持久化字段级数标绑定 (`field_standard_bindings`)，无缝供给「2. 落标绑定与贯标」与「3. 跨环境比对」共享复用。
"""

from __future__ import annotations

import datetime
import io
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.excel_handler import validate_xlsx_bytes
from core.platform.dialects import clean_identifier_input, is_cross_type_compatible
from core.platform.storage import PlatformStateStore

# 严格对齐渤海银行标准数据字典模板的 46 列表头顺序（索引 23 与 44 同名为「仓库DWS数据格式」）
BOHAI_DICT_HEADERS: Tuple[str, ...] = (
    "数据项编号",
    "监管分类",
    "一级分类",
    "二级分类",
    "三级分类",
    "中文名称",
    "中文别名",
    "英文名称",
    "英文简称",
    "业务定义",
    "适用范围",
    "业务规则",
    "最大录入长度",
    "最小录入精度",
    "引用代码编号",
    "引用代码中文名称",
    "数据安全等级",
    "引用标准编号",
    "引用标准名称",
    "引用域中文名",
    "引用域编号",
    "Mysql物理类型",
    "Oracle物理类型",
    "仓库DWS数据格式",
    "DaMeng数据格式",
    "GoldenDb(MySQL模式)",
    "制定依据系统",
    "是否同步到DDM",
    "是否同步到lunaweb",
    "是否为标准字典",
    "非标准的父标准",
    "DDM目录",
    "引用代码详情",
    "字典类型",
    "字典级别",
    "版本号",
    "数据类别",
    "数据格式",
    "取值范围",
    "ocenbase-oracle数据格式",
    "ocenbase-mysql数据格式",
    "argodb数据格式",
    "ocenbase数据格式",
    "数据仓库 集市",
    "仓库DWS数据格式",
    "备注",
)

FORMAT_PROFILES: Tuple[Dict[str, str], ...] = (
    {"value": "AUTO", "label": "自动按库匹配"},
    {"value": "type_dws", "label": "仓库DWS数据格式"},
    {"value": "type_dw_mart", "label": "数据仓库 集市"},
    {"value": "type_argodb", "label": "argodb数据格式"},
    {"value": "type_mysql", "label": "Mysql物理类型"},
    {"value": "type_oracle", "label": "Oracle物理类型"},
    {"value": "type_ob_oracle", "label": "ocenbase-oracle数据格式"},
    {"value": "type_ob_mysql", "label": "ocenbase-mysql数据格式"},
    {"value": "type_oceanbase", "label": "ocenbase数据格式"},
    {"value": "type_dameng", "label": "DaMeng数据格式"},
    {"value": "type_goldendb", "label": "GoldenDb(MySQL模式)"},
)

VALID_FORMAT_PROFILE_VALUES = {p["value"] for p in FORMAT_PROFILES}

# SQLite 表专用列清单（与 storage.py standard_dictionary 对齐）
DICT_COLUMNS: Tuple[str, ...] = (
    "std_code",
    "reg_category",
    "cat_l1",
    "cat_l2",
    "cat_l3",
    "cn_name",
    "cn_alias",
    "en_name",
    "en_abbr",
    "biz_definition",
    "scope_desc",
    "biz_rule",
    "max_length",
    "min_scale",
    "ref_code_no",
    "ref_code_name",
    "ref_code_detail",
    "security_level",
    "ref_std_no",
    "ref_std_name",
    "domain_name",
    "domain_code",
    "data_category",
    "data_format",
    "value_range",
    "source_system",
    "ddm_catalog",
    "is_standard_dict",
    "version_no",
    "remark",
    "type_dws",
    "type_dw_mart",
    "type_mysql",
    "type_oracle",
    "type_dameng",
    "type_goldendb",
    "type_ob_oracle",
    "type_ob_mysql",
    "type_argodb",
    "type_oceanbase",
    "extra_attrs_json",
    "updated_at",
)

# 表头标准化名 -> 内部字段键映射（支持 46 列全称及精简模板同义词）
_HEADER_TO_FIELD: Dict[str, str] = {
    "数据项编号": "std_code",
    "标准编号": "std_code",
    "数标编号": "std_code",
    "stdcode": "std_code",
    "监管分类": "reg_category",
    "一级分类": "cat_l1",
    "二级分类": "cat_l2",
    "三级分类": "cat_l3",
    "中文名称": "cn_name",
    "标准中文名称": "cn_name",
    "标准中文名": "cn_name",
    "cnname": "cn_name",
    "中文别名": "cn_alias",
    "英文名称": "en_name",
    "enname": "en_name",
    "英文简称": "en_abbr",
    "标准英文简称": "en_abbr",
    "标准英文名": "en_abbr",
    "标准字段名": "en_abbr",
    "enabbr": "en_abbr",
    "业务定义": "biz_definition",
    "适用范围": "scope_desc",
    "业务规则": "biz_rule",
    "最大录入长度": "max_length",
    "最小录入精度": "min_scale",
    "引用代码编号": "ref_code_no",
    "引用代码中文名称": "ref_code_name",
    "引用代码详情": "ref_code_detail",
    "数据安全等级": "security_level",
    "引用标准编号": "ref_std_no",
    "引用标准名称": "ref_std_name",
    "引用域中文名": "domain_name",
    "引用域编号": "domain_code",
    "数据类别": "data_category",
    "数据格式": "data_format",
    "取值范围": "value_range",
    "制定依据系统": "source_system",
    "ddm目录": "ddm_catalog",
    "是否为标准字典": "is_standard_dict",
    "版本号": "version_no",
    "备注": "remark",
    "仓库dws数据格式": "type_dws",
    "dws物理类型": "type_dws",
    "typedws": "type_dws",
    "数据仓库集市": "type_dw_mart",
    "typedwmart": "type_dw_mart",
    "mysql物理类型": "type_mysql",
    "mysql数据格式": "type_mysql",
    "typemysql": "type_mysql",
    "oracle物理类型": "type_oracle",
    "oracle数据格式": "type_oracle",
    "typeoracle": "type_oracle",
    "dameng数据格式": "type_dameng",
    "达梦物理类型": "type_dameng",
    "typedameng": "type_dameng",
    "goldendb(mysql模式)": "type_goldendb",
    "typegoldendb": "type_goldendb",
    "ocenbaseoracle数据格式": "type_ob_oracle",
    "oceanbaseoracle数据格式": "type_ob_oracle",
    "typeoboracle": "type_ob_oracle",
    "ocenbasemysql数据格式": "type_ob_mysql",
    "oceanbasemysql数据格式": "type_ob_mysql",
    "typeobmysql": "type_ob_mysql",
    "argodb数据格式": "type_argodb",
    "inceptor数据格式": "type_argodb",
    "typeargodb": "type_argodb",
    "ocenbase数据格式": "type_oceanbase",
    "oceanbase数据格式": "type_oceanbase",
    "typeoceanbase": "type_oceanbase",
    "是否同步到ddm": "extra::sync_ddm",
    "是否同步到lunaweb": "extra::sync_lunaweb",
    "非标准的父标准": "extra::non_std_parent",
    "字典类型": "extra::dict_type",
    "字典级别": "extra::dict_level",
}

_INVALID_PLACEHOLDERS = {"/", "-", "--", "none", "null", "#n/a", "#value!", "#ref!"}


def _norm_hdr(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    s = s.replace("（", "(").replace("）", ")")
    for ch in (" ", "\t", "\n", "\r", "_", "-", "\u200b", "\u3000"):
        s = s.replace(ch, "")
    return s


def _clean_cell(val: Any, *, treat_slash_empty: bool = False) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and val.is_integer():
        s = str(int(val))
    else:
        s = str(val).replace("\u200b", "").replace("\ufeff", "").strip()
    if not s:
        return ""
    if s.lower() == "#n/a":
        return ""
    if treat_slash_empty and s.lower() in _INVALID_PLACEHOLDERS:
        return ""
    return s


def _safe_excel_cell(val: Any) -> Any:
    if isinstance(val, str) and val[:1] in ("=", "+", "-", "@"):
        return "'" + val
    return val


def _style_sheet(ws: Any, col_count: int, required_indices: Optional[set[int]] = None) -> None:
    req_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    opt_fill = PatternFill(start_color="334155", end_color="334155", fill_type="solid")
    header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    body_font = Font(name="Microsoft YaHei", size=10)
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )
    req = required_indices or set()
    for c_idx in range(1, col_count + 1):
        cell = ws.cell(row=1, column=c_idx)
        cell.fill = req_fill if c_idx in req else opt_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        col_letter = get_column_letter(c_idx)
        title_len = len(str(cell.value or ""))
        ws.column_dimensions[col_letter].width = max(14, min(32, title_len * 2 + 4))
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=col_count):
        for cell in row:
            cell.font = body_font
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")


class StandardCatalog:
    """平台全局数据标准字典、物理类型解析、字段绑定与落标核验服务."""

    def __init__(self, store: PlatformStateStore) -> None:
        self.store = store

    @staticmethod
    def _now_str() -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def list_format_profiles() -> List[Dict[str, str]]:
        return [dict(p) for p in FORMAT_PROFILES]

    @staticmethod
    def _row_to_item(row: Any) -> Dict[str, Any]:
        d = dict(row)
        try:
            extra = json.loads(d.pop("extra_attrs_json", "{}") or "{}")
            if not isinstance(extra, dict):
                extra = {}
        except Exception:
            extra = {}
        d["extra_attrs"] = extra
        return d

    def get_summary(self) -> Dict[str, Any]:
        with self.store.module_db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM standard_dictionary").fetchone()[0]
            reg_rows = conn.execute(
                "SELECT reg_category, COUNT(*) AS cnt FROM standard_dictionary "
                "WHERE reg_category != '' GROUP BY reg_category ORDER BY cnt DESC LIMIT 30"
            ).fetchall()
            cat_rows = conn.execute(
                "SELECT data_category, COUNT(*) AS cnt FROM standard_dictionary "
                "WHERE data_category != '' GROUP BY data_category ORDER BY cnt DESC LIMIT 30"
            ).fetchall()
        return {
            "total_count": int(total),
            "filename": self.store.get_meta("std_dict_filename", "未导入"),
            "imported_at": self.store.get_meta("std_dict_imported_at", ""),
            "reg_categories": [
                {"value": r["reg_category"], "count": int(r["cnt"])} for r in reg_rows
            ],
            "data_categories": [
                {"value": r["data_category"], "count": int(r["cnt"])} for r in cat_rows
            ],
        }

    def get_item(self, std_code: str) -> Optional[Dict[str, Any]]:
        code = (std_code or "").strip()
        if not code:
            return None
        with self.store.module_db() as conn:
            row = conn.execute(
                "SELECT * FROM standard_dictionary WHERE std_code = ?",
                (code,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM standard_dictionary WHERE LOWER(std_code) = LOWER(?)",
                    (code,),
                ).fetchone()
            return self._row_to_item(row) if row else None

    def get_items_by_codes(self, std_codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        codes = [str(c).strip() for c in std_codes if str(c or "").strip()]
        if not codes:
            return {}
        unique_codes = list(dict.fromkeys(codes))
        result: Dict[str, Dict[str, Any]] = {}
        with self.store.module_db() as conn:
            chunk_size = 500
            for i in range(0, len(unique_codes), chunk_size):
                chunk = unique_codes[i : i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM standard_dictionary WHERE std_code IN ({placeholders})",
                    chunk,
                ).fetchall()
                for r in rows:
                    item = self._row_to_item(r)
                    result[item["std_code"]] = item
        return result

    def search_standards(
        self,
        *,
        keyword: str = "",
        reg_category: str = "",
        data_category: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        kw = (keyword or "").strip()
        reg = (reg_category or "").strip()
        dcat = (data_category or "").strip()
        page = max(1, int(page or 1))
        page_size = max(1, min(200, int(page_size or 50)))

        clauses: List[str] = []
        params: List[Any] = []
        if kw:
            like_kw = f"%{kw}%"
            clauses.append(
                "(std_code LIKE ? OR cn_name LIKE ? OR en_abbr LIKE ? OR en_name LIKE ? OR biz_definition LIKE ? OR domain_name LIKE ?)"
            )
            params.extend([like_kw] * 6)
        if reg:
            clauses.append("reg_category = ?")
            params.append(reg)
        if dcat:
            clauses.append("data_category = ?")
            params.append(dcat)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        offset = (page - 1) * page_size

        with self.store.module_db() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM standard_dictionary {where_sql}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM standard_dictionary {where_sql} "
                "ORDER BY CASE WHEN ? != '' AND LOWER(std_code) = LOWER(?) THEN 0 "
                "WHEN ? != '' AND LOWER(en_abbr) = LOWER(?) THEN 1 ELSE 2 END, std_code ASC "
                "LIMIT ? OFFSET ?",
                [*params, kw, kw, kw, kw, page_size, offset],
            ).fetchall()

        return {
            "items": [self._row_to_item(r) for r in rows],
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (int(total) + page_size - 1) // page_size),
        }

    def lookup_standards(self, keyword: str = "", limit: int = 30) -> List[Dict[str, Any]]:
        kw = (keyword or "").strip()
        limit = max(1, min(100, int(limit or 30)))
        with self.store.module_db() as conn:
            if not kw:
                rows = conn.execute(
                    "SELECT * FROM standard_dictionary ORDER BY std_code ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                like_kw = f"%{kw}%"
                rows = conn.execute(
                    """
                    SELECT * FROM standard_dictionary
                    WHERE std_code LIKE ? OR cn_name LIKE ? OR en_abbr LIKE ? OR domain_name LIKE ?
                    ORDER BY
                        CASE
                            WHEN LOWER(std_code) = LOWER(?) THEN 0
                            WHEN LOWER(en_abbr) = LOWER(?) THEN 1
                            WHEN cn_name = ? THEN 2
                            WHEN LOWER(en_abbr) LIKE LOWER(?) THEN 3
                            ELSE 4
                        END,
                        std_code ASC
                    LIMIT ?
                    """,
                    (like_kw, like_kw, like_kw, like_kw, kw, kw, kw, f"{kw}%", limit),
                ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def upsert_item(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        std_code = _clean_cell(payload.get("std_code"))
        cn_name = _clean_cell(payload.get("cn_name"))
        en_abbr = clean_identifier_input(_clean_cell(payload.get("en_abbr")))
        if not std_code:
            raise ValueError("数据项编号 (std_code) 不能为空")
        if not cn_name:
            raise ValueError("中文名称不能为空")
        if not en_abbr:
            raise ValueError("英文简称不能为空")

        now = self._now_str()
        extra = payload.get("extra_attrs")
        if not isinstance(extra, dict):
            extra = {}

        row_vals = (
            std_code,
            _clean_cell(payload.get("reg_category")),
            _clean_cell(payload.get("cat_l1")),
            _clean_cell(payload.get("cat_l2")),
            _clean_cell(payload.get("cat_l3")),
            cn_name,
            _clean_cell(payload.get("cn_alias")),
            _clean_cell(payload.get("en_name")) or en_abbr,
            en_abbr,
            _clean_cell(payload.get("biz_definition")),
            _clean_cell(payload.get("scope_desc")),
            _clean_cell(payload.get("biz_rule")),
            _clean_cell(payload.get("max_length"), treat_slash_empty=True),
            _clean_cell(payload.get("min_scale"), treat_slash_empty=True),
            _clean_cell(payload.get("ref_code_no")),
            _clean_cell(payload.get("ref_code_name")),
            _clean_cell(payload.get("ref_code_detail")),
            _clean_cell(payload.get("security_level")),
            _clean_cell(payload.get("ref_std_no")),
            _clean_cell(payload.get("ref_std_name")),
            _clean_cell(payload.get("domain_name")),
            _clean_cell(payload.get("domain_code")),
            _clean_cell(payload.get("data_category")),
            _clean_cell(payload.get("data_format"), treat_slash_empty=True),
            _clean_cell(payload.get("value_range"), treat_slash_empty=True),
            _clean_cell(payload.get("source_system")),
            _clean_cell(payload.get("ddm_catalog")),
            _clean_cell(payload.get("is_standard_dict")) or "1",
            _clean_cell(payload.get("version_no")) or "1",
            _clean_cell(payload.get("remark")),
            _clean_cell(payload.get("type_dws"), treat_slash_empty=True),
            _clean_cell(payload.get("type_dw_mart"), treat_slash_empty=True),
            _clean_cell(payload.get("type_mysql"), treat_slash_empty=True),
            _clean_cell(payload.get("type_oracle"), treat_slash_empty=True),
            _clean_cell(payload.get("type_dameng"), treat_slash_empty=True),
            _clean_cell(payload.get("type_goldendb"), treat_slash_empty=True),
            _clean_cell(payload.get("type_ob_oracle"), treat_slash_empty=True),
            _clean_cell(payload.get("type_ob_mysql"), treat_slash_empty=True),
            _clean_cell(payload.get("type_argodb"), treat_slash_empty=True),
            _clean_cell(payload.get("type_oceanbase"), treat_slash_empty=True),
            json.dumps(extra, ensure_ascii=False),
            now,
        )
        col_list = ", ".join(DICT_COLUMNS)
        placeholders = ", ".join("?" for _ in DICT_COLUMNS)
        update_cols = ", ".join(
            f"{c} = excluded.{c}" for c in DICT_COLUMNS if c != "std_code"
        )
        with self.store.module_db() as conn:
            conn.execute(
                f"INSERT INTO standard_dictionary ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT(std_code) DO UPDATE SET {update_cols}",
                row_vals,
            )
        return self.get_item(std_code) or {}

    def update_item(self, old_std_code: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        old_code = _clean_cell(old_std_code)
        new_code = _clean_cell(payload.get("std_code") or old_code)
        if not old_code:
            raise ValueError("待修改的数据项编号不能为空")
        merged = {**payload, "std_code": new_code}
        if new_code != old_code:
            with self.store.module_db() as conn:
                conn.execute("DELETE FROM standard_dictionary WHERE std_code = ?", (old_code,))
                conn.execute(
                    "UPDATE field_standard_bindings SET std_code = ? WHERE std_code = ?",
                    (new_code, old_code),
                )
                conn.execute(
                    "UPDATE cross_column_bindings SET source_std_code = ? WHERE source_std_code = ?",
                    (new_code, old_code),
                )
                conn.execute(
                    "UPDATE cross_column_bindings SET target_std_code = ? WHERE target_std_code = ?",
                    (new_code, old_code),
                )
        return self.upsert_item(merged)

    def delete_item(self, std_code: str) -> bool:
        code = _clean_cell(std_code)
        if not code:
            return False
        with self.store.module_db() as conn:
            cur = conn.execute("DELETE FROM standard_dictionary WHERE std_code = ?", (code,))
            return cur.rowcount > 0

    def clear_dictionary(self) -> None:
        with self.store.module_db() as conn:
            conn.execute("DELETE FROM standard_dictionary")
        self.store.save_meta("std_dict_filename", "未导入")
        self.store.save_meta("std_dict_imported_at", "")

    @staticmethod
    def parse_standard_dictionary_excel(filename: str, content: bytes) -> List[Dict[str, Any]]:
        """流式解析标准字典 Excel（兼容 46 列渤海模板及精简标准表头，处理同名 DWS 格式列）."""
        validate_xlsx_bytes(filename, content)
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            if not wb.worksheets:
                raise ValueError("Excel 文件中未找到有效工作表")

            parsed: List[Dict[str, Any]] = []
            seen_order: Dict[str, int] = {}

            for ws in wb.worksheets:
                if hasattr(ws, "reset_dimensions"):
                    try:
                        ws.reset_dimensions()
                    except Exception:
                        pass

                row_iter = ws.iter_rows(values_only=True)
                header_indices: Optional[Dict[str, List[int]]] = None
                extra_col_indices: List[Tuple[str, int]] = []
                unknown_col_indices: List[Tuple[str, int]] = []

                for _probe_idx in range(25):
                    try:
                        row = next(row_iter)
                    except StopIteration:
                        break
                    if not row:
                        continue
                    idx_map: Dict[str, List[int]] = {}
                    extras: List[Tuple[str, int]] = []
                    unknowns: List[Tuple[str, int]] = []
                    for c_idx, cell in enumerate(row):
                        raw_h = _clean_cell(cell)
                        if not raw_h:
                            continue
                        norm_h = _norm_hdr(raw_h)
                        mapped = _HEADER_TO_FIELD.get(norm_h)
                        if mapped:
                            if mapped.startswith("extra::"):
                                extras.append((mapped.split("::", 1)[1], c_idx))
                            else:
                                idx_map.setdefault(mapped, []).append(c_idx)
                        else:
                            unknowns.append((raw_h, c_idx))

                    if "std_code" in idx_map and ("cn_name" in idx_map or "en_abbr" in idx_map):
                        header_indices = idx_map
                        extra_col_indices = extras
                        unknown_col_indices = unknowns
                        break

                if not header_indices:
                    continue

                def _pick_first(row_vals: Sequence[Any], field: str, *, slash_empty: bool = False) -> str:
                    for c_i in header_indices.get(field, []):
                        if c_i < len(row_vals):
                            v = _clean_cell(row_vals[c_i], treat_slash_empty=slash_empty)
                            if v:
                                return v
                    return ""

                def _pick_rightmost(
                    row_vals: Sequence[Any],
                    field: str,
                    *,
                    header_literal: str = "",
                ) -> str:
                    # 解决第 24 列与第 45 列同名「仓库DWS数据格式」：从右向左取第一个有效物理格式
                    for c_i in reversed(header_indices.get(field, [])):
                        if c_i < len(row_vals):
                            v = _clean_cell(row_vals[c_i], treat_slash_empty=True)
                            if v and v != header_literal:
                                return v
                    return ""

                for row in row_iter:
                    if not row:
                        continue
                    std_code = _pick_first(row, "std_code")
                    if not std_code or std_code in ("数据项编号", "标准编号", "std_code"):
                        continue

                    cn_name = _pick_first(row, "cn_name")
                    en_abbr_raw = _pick_first(row, "en_abbr") or _pick_first(row, "en_name")
                    en_abbr = clean_identifier_input(en_abbr_raw) or en_abbr_raw
                    if not cn_name and not en_abbr:
                        continue

                    # 检查第 24 列遗留格式列，若存在独立值一并存入 extra_attrs 保真
                    extra_attrs: Dict[str, str] = {}
                    dws_cols = header_indices.get("type_dws", [])
                    if len(dws_cols) > 1 and dws_cols[0] < len(row):
                        legacy_dws = _clean_cell(row[dws_cols[0]], treat_slash_empty=True)
                        if legacy_dws and legacy_dws != "仓库DWS数据格式":
                            extra_attrs["legacy_type_dws"] = legacy_dws
                    for ek, c_i in extra_col_indices:
                        if c_i < len(row):
                            ev = _clean_cell(row[c_i])
                            if ev:
                                extra_attrs[ek] = ev
                    for uk, c_i in unknown_col_indices:
                        if c_i < len(row):
                            uv = _clean_cell(row[c_i])
                            if uv:
                                extra_attrs[uk] = uv

                    item = {
                        "std_code": std_code,
                        "reg_category": _pick_first(row, "reg_category"),
                        "cat_l1": _pick_first(row, "cat_l1"),
                        "cat_l2": _pick_first(row, "cat_l2"),
                        "cat_l3": _pick_first(row, "cat_l3"),
                        "cn_name": cn_name or en_abbr,
                        "cn_alias": _pick_first(row, "cn_alias"),
                        "en_name": _pick_first(row, "en_name") or en_abbr,
                        "en_abbr": en_abbr,
                        "biz_definition": _pick_first(row, "biz_definition"),
                        "scope_desc": _pick_first(row, "scope_desc"),
                        "biz_rule": _pick_first(row, "biz_rule"),
                        "max_length": _pick_first(row, "max_length", slash_empty=True),
                        "min_scale": _pick_first(row, "min_scale", slash_empty=True),
                        "ref_code_no": _pick_first(row, "ref_code_no"),
                        "ref_code_name": _pick_first(row, "ref_code_name"),
                        "ref_code_detail": _pick_first(row, "ref_code_detail"),
                        "security_level": _pick_first(row, "security_level"),
                        "ref_std_no": _pick_first(row, "ref_std_no"),
                        "ref_std_name": _pick_first(row, "ref_std_name"),
                        "domain_name": _pick_first(row, "domain_name"),
                        "domain_code": _pick_first(row, "domain_code"),
                        "data_category": _pick_first(row, "data_category"),
                        "data_format": _pick_first(row, "data_format", slash_empty=True),
                        "value_range": _pick_first(row, "value_range", slash_empty=True),
                        "source_system": _pick_first(row, "source_system"),
                        "ddm_catalog": _pick_first(row, "ddm_catalog"),
                        "is_standard_dict": _pick_first(row, "is_standard_dict"),
                        "version_no": _pick_first(row, "version_no"),
                        "remark": _pick_first(row, "remark"),
                        "type_dws": _pick_rightmost(row, "type_dws", header_literal="仓库DWS数据格式"),
                        "type_dw_mart": _pick_rightmost(row, "type_dw_mart", header_literal="数据仓库 集市"),
                        "type_mysql": _pick_rightmost(row, "type_mysql", header_literal="Mysql物理类型"),
                        "type_oracle": _pick_rightmost(row, "type_oracle", header_literal="Oracle物理类型"),
                        "type_dameng": _pick_rightmost(row, "type_dameng", header_literal="DaMeng数据格式"),
                        "type_goldendb": _pick_rightmost(row, "type_goldendb", header_literal="GoldenDb(MySQL模式)"),
                        "type_ob_oracle": _pick_rightmost(row, "type_ob_oracle", header_literal="ocenbase-oracle数据格式"),
                        "type_ob_mysql": _pick_rightmost(row, "type_ob_mysql", header_literal="ocenbase-mysql数据格式"),
                        "type_argodb": _pick_rightmost(row, "type_argodb", header_literal="argodb数据格式"),
                        "type_oceanbase": _pick_rightmost(row, "type_oceanbase", header_literal="ocenbase数据格式"),
                        "extra_attrs": extra_attrs,
                    }

                    if std_code in seen_order:
                        parsed[seen_order[std_code]] = item
                    else:
                        seen_order[std_code] = len(parsed)
                        parsed.append(item)

            if not parsed:
                raise ValueError(
                    "未能从 Excel 解析到有效的数据标准记录，请使用系统模板（需包含「数据项编号」「中文名称」「英文简称」）"
                )
            return parsed
        finally:
            wb.close()

    def import_dictionary_excel(
        self,
        filename: str,
        content: bytes,
        *,
        mode: str = "upsert",
    ) -> Dict[str, Any]:
        eff_mode = (mode or "upsert").strip().lower()
        if eff_mode not in ("upsert", "replace"):
            eff_mode = "upsert"

        parsed = self.parse_standard_dictionary_excel(filename, content)
        now = self._now_str()

        tuples_to_write = [
            (
                it["std_code"],
                it["reg_category"],
                it["cat_l1"],
                it["cat_l2"],
                it["cat_l3"],
                it["cn_name"],
                it["cn_alias"],
                it["en_name"],
                it["en_abbr"],
                it["biz_definition"],
                it["scope_desc"],
                it["biz_rule"],
                it["max_length"],
                it["min_scale"],
                it["ref_code_no"],
                it["ref_code_name"],
                it["ref_code_detail"],
                it["security_level"],
                it["ref_std_no"],
                it["ref_std_name"],
                it["domain_name"],
                it["domain_code"],
                it["data_category"],
                it["data_format"],
                it["value_range"],
                it["source_system"],
                it["ddm_catalog"],
                it["is_standard_dict"],
                it["version_no"],
                it["remark"],
                it["type_dws"],
                it["type_dw_mart"],
                it["type_mysql"],
                it["type_oracle"],
                it["type_dameng"],
                it["type_goldendb"],
                it["type_ob_oracle"],
                it["type_ob_mysql"],
                it["type_argodb"],
                it["type_oceanbase"],
                json.dumps(it.get("extra_attrs") or {}, ensure_ascii=False),
                now,
            )
            for it in parsed
        ]

        col_list = ", ".join(DICT_COLUMNS)
        placeholders = ", ".join("?" for _ in DICT_COLUMNS)
        update_cols = ", ".join(
            f"{c} = excluded.{c}" for c in DICT_COLUMNS if c != "std_code"
        )

        with self.store.module_db() as conn:
            if eff_mode == "replace":
                conn.execute("DELETE FROM standard_dictionary")
            conn.executemany(
                f"INSERT INTO standard_dictionary ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT(std_code) DO UPDATE SET {update_cols}",
                tuples_to_write,
            )

        self.store.save_meta("std_dict_filename", filename)
        self.store.save_meta("std_dict_imported_at", now)
        self.store.add_log(
            "SUCCESS",
            "标准字典导入",
            f"已按{'全量覆盖' if eff_mode == 'replace' else '增量合并'}模式导入标准字典 [{filename}]，共 {len(tuples_to_write)} 条",
        )
        return {
            "imported_count": len(tuples_to_write),
            "mode": eff_mode,
            "summary": self.get_summary(),
        }

    @staticmethod
    def generate_dictionary_template_bytes() -> bytes:
        """生成严格对齐 46 列渤海数据字典结构的脱敏导入模板."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "数据字典"
        ws.append(list(BOHAI_DICT_HEADERS))

        def _row(
            *,
            code: str,
            reg: str,
            cn: str,
            en_name: str,
            en_abbr: str,
            biz_def: str,
            rule: str,
            max_len: str,
            min_scale: str,
            domain_cn: str,
            domain_cd: str,
            data_cat: str,
            t_mysql: str,
            t_oracle: str,
            t_ob_ora: str,
            t_ob_my: str,
            t_argo: str,
            t_ob: str,
            t_mart: str,
            t_dws: str,
            sys_src: str,
        ) -> List[str]:
            return [
                code, reg, "", "", "", cn, "", en_name, en_abbr, biz_def,
                "全行基础数据", rule, max_len, min_scale, "", "", "", "", "",
                domain_cn, domain_cd, t_mysql, t_oracle, "", "", "", sys_src,
                "1", "1", "1", "", "基础标准:基础标准/数据字典", "", "", "", "1",
                data_cat, "/", "/", t_ob_ora, t_ob_my, t_argo, t_ob, t_mart, t_dws, "",
            ]

        samples = [
            _row(
                code="CUST000101",
                reg="业务数据",
                cn="客户统一编号",
                en_name="Customer Unified Identifier",
                en_abbr="CUST_ID",
                biz_def="全行对公与零售客户唯一识别编号",
                rule="由客户中心统一生成，不可为空",
                max_len="32",
                min_scale="",
                domain_cn="编码类32",
                domain_cd="Y100001",
                data_cat="编码类",
                t_mysql="VARCHAR(32)",
                t_oracle="VARCHAR2(32)",
                t_ob_ora="VARCHAR2(32)",
                t_ob_my="VARCHAR(32)",
                t_argo="VARCHAR(32)",
                t_ob="NVARCHAR(32)",
                t_mart="VARCHAR2(50)",
                t_dws="NVARCHAR2(50)",
                sys_src="客户信息中心",
            ),
            _row(
                code="ACCT000205",
                reg="业务数据",
                cn="账户开户日期",
                en_name="Account Open Date",
                en_abbr="OPEN_DT",
                biz_def="账户正式设立并启用核算的日期",
                rule="格式 YYYY-MM-DD",
                max_len="10",
                min_scale="",
                domain_cn="日期类",
                domain_cd="Y100020",
                data_cat="日期时间类",
                t_mysql="DATE",
                t_oracle="DATE",
                t_ob_ora="DATE",
                t_ob_my="DATE",
                t_argo="DATE",
                t_ob="DATE",
                t_mart="DATE",
                t_dws="DATE",
                sys_src="存款中心",
            ),
            _row(
                code="TXN000309",
                reg="业务数据",
                cn="交易发生金额",
                en_name="Transaction Amount",
                en_abbr="TXN_AMT",
                biz_def="单笔金融交易实际发生的本位币或原币金额",
                rule="保留两位小数",
                max_len="18",
                min_scale="2",
                domain_cn="金额类(18,2)",
                domain_cd="Y100089",
                data_cat="金额类",
                t_mysql="DECIMAL(18,2)",
                t_oracle="DECIMAL(18,2)",
                t_ob_ora="DECIMAL(18,2)",
                t_ob_my="DECIMAL(18,2)",
                t_argo="DECIMAL(18,2)",
                t_ob="DECIMAL(18,2)",
                t_mart="DECIMAL(18,2)",
                t_dws="DECIMAL(18,2)",
                sys_src="支付结算中心",
            ),
            _row(
                code="COM000402",
                reg="技术数据",
                cn="有效标志",
                en_name="Valid Indicator",
                en_abbr="VALID_IND",
                biz_def="标识当前数据记录是否处于有效状态",
                rule="0-无效 1-有效",
                max_len="1",
                min_scale="0",
                domain_cn="指示器类",
                domain_cd="Y100111",
                data_cat="指示器类",
                t_mysql="CHAR(1)",
                t_oracle="CHAR(1)",
                t_ob_ora="CHAR(1)",
                t_ob_my="CHAR(1)",
                t_argo="CHAR(1)",
                t_ob="CHAR(1)",
                t_mart="VARCHAR2(10)",
                t_dws="NVARCHAR2(10)",
                sys_src="公共能力中心",
            ),
        ]
        for s in samples:
            ws.append(s)

        # 必填核心列：数据项编号(1), 中文名称(6), 英文简称(9), 仓库DWS数据格式(45)
        _style_sheet(ws, len(BOHAI_DICT_HEADERS), required_indices={1, 6, 9, 45})

        ws_note = wb.create_sheet(title="填写说明")
        ws_note.append(["列名称", "是否必填", "填写说明"])
        notes = [
            ["数据项编号", "必填", "全局唯一数据标准编码，系统以此作为主键进行增量合并与关联绑定"],
            ["中文名称", "必填", "数据标准中文业务名称，将用于字段注释一致性校验"],
            ["英文简称", "必填", "标准英文物理字段简称，将用于字段命名一致性校验"],
            ["仓库DWS数据格式", "推荐必填", "数仓 DWS 默认采用最右侧第 45 列「仓库DWS数据格式」；如存在多库类型列，系统一并完整入库"],
            ["多库物理类型列", "选填", "支持 Mysql、Oracle、DaMeng、GoldenDb、OceanBase、ArgoDB、数据仓库 集市 等全部格式列"],
            ["导入模式说明", "提示", "增量合并模式会按「数据项编号」更新字典并自动保留库表字段既有绑定；全量覆盖模式仅重置字典表"],
        ]
        for n in notes:
            ws_note.append(n)
        _style_sheet(ws_note, 3, required_indices={1})
        ws_note.column_dimensions["A"].width = 24
        ws_note.column_dimensions["B"].width = 14
        ws_note.column_dimensions["C"].width = 72

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def export_dictionary_excel_bytes(
        self,
        *,
        keyword: str = "",
        reg_category: str = "",
        data_category: str = "",
    ) -> bytes:
        """导出当前标准字典（严格还原 46 列结构）."""
        clauses: List[str] = []
        params: List[Any] = []
        if keyword.strip():
            lk = f"%{keyword.strip()}%"
            clauses.append(
                "(std_code LIKE ? OR cn_name LIKE ? OR en_abbr LIKE ? OR biz_definition LIKE ?)"
            )
            params.extend([lk] * 4)
        if reg_category.strip():
            clauses.append("reg_category = ?")
            params.append(reg_category.strip())
        if data_category.strip():
            clauses.append("data_category = ?")
            params.append(data_category.strip())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self.store.module_db() as conn:
            rows = conn.execute(
                f"SELECT * FROM standard_dictionary {where_sql} ORDER BY std_code ASC",
                params,
            ).fetchall()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "数据字典"
        ws.append(list(BOHAI_DICT_HEADERS))

        for r in rows:
            item = self._row_to_item(r)
            ex = item.get("extra_attrs") or {}
            out_row = [
                item["std_code"],
                item["reg_category"],
                item["cat_l1"],
                item["cat_l2"],
                item["cat_l3"],
                item["cn_name"],
                item["cn_alias"],
                item["en_name"],
                item["en_abbr"],
                item["biz_definition"],
                item["scope_desc"],
                item["biz_rule"],
                item["max_length"],
                item["min_scale"],
                item["ref_code_no"],
                item["ref_code_name"],
                item["security_level"],
                item["ref_std_no"],
                item["ref_std_name"],
                item["domain_name"],
                item["domain_code"],
                item["type_mysql"],
                item["type_oracle"],
                ex.get("legacy_type_dws", ""),
                item["type_dameng"],
                item["type_goldendb"],
                item["source_system"],
                ex.get("sync_ddm", ""),
                ex.get("sync_lunaweb", ""),
                item["is_standard_dict"],
                ex.get("non_std_parent", ""),
                item["ddm_catalog"],
                item["ref_code_detail"],
                ex.get("dict_type", ""),
                ex.get("dict_level", ""),
                item["version_no"],
                item["data_category"],
                item["data_format"],
                item["value_range"],
                item["type_ob_oracle"],
                item["type_ob_mysql"],
                item["type_argodb"],
                item["type_oceanbase"],
                item["type_dw_mart"],
                item["type_dws"],
                item["remark"],
            ]
            ws.append([_safe_excel_cell(v) for v in out_row])

        _style_sheet(ws, len(BOHAI_DICT_HEADERS), required_indices={1, 6, 9, 45})
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    @staticmethod
    def resolve_physical_type(
        std_item: Dict[str, Any],
        *,
        db_type: str = "DWS",
        format_profile: str = "AUTO",
    ) -> str:
        """按数据源类型与指定格式档智能解析标准目标物理数据类型."""
        prof = (format_profile or "AUTO").strip()
        if prof in VALID_FORMAT_PROFILE_VALUES and prof != "AUTO":
            picked = _clean_cell(std_item.get(prof), treat_slash_empty=True)
            if picked:
                return picked

        dt = (db_type or "DWS").strip().upper()
        if dt in ("DWS", "PGSQL", "KINGBASE"):
            chain = ("type_dws", "type_dw_mart", "type_oracle", "type_ob_oracle", "type_mysql")
        elif dt == "INCEPTOR":
            chain = ("type_argodb", "type_dws", "type_mysql", "type_ob_mysql")
        elif dt == "MYSQL":
            chain = ("type_mysql", "type_ob_mysql", "type_oceanbase", "type_goldendb", "type_dws")
        elif dt == "ORACLE":
            chain = ("type_oracle", "type_ob_oracle", "type_dw_mart", "type_dws")
        elif dt == "DM8":
            chain = ("type_dameng", "type_oracle", "type_ob_oracle", "type_dws")
        else:
            chain = ("type_mysql", "type_ob_mysql", "type_dws", "type_dw_mart")

        for k in chain:
            val = _clean_cell(std_item.get(k), treat_slash_empty=True)
            if val:
                return val

        # 若物理格式列全空，按最大长度与精度兜底推导
        dcat = str(std_item.get("data_category") or "")
        max_len = _clean_cell(std_item.get("max_length"), treat_slash_empty=True)
        min_scale = _clean_cell(std_item.get("min_scale"), treat_slash_empty=True)
        if "日期" in dcat:
            return "DATE"
        if "时间" in dcat:
            return "TIMESTAMP"
        if any(k in dcat for k in ("金额", "数值", "比例")) and max_len.isdigit():
            sc = min_scale if min_scale.isdigit() else "2"
            return f"DECIMAL({max_len},{sc})"
        if max_len.isdigit():
            return f"VARCHAR({max_len})"
        return "VARCHAR(64)"

    @classmethod
    def evaluate_column_compliance(
        cls,
        *,
        col_name: str,
        col_type: str,
        col_comment: str,
        std_item: Optional[Dict[str, Any]],
        std_code: str = "",
        db_type: str = "DWS",
        format_profile: str = "AUTO",
    ) -> Dict[str, Any]:
        """三维分级落标核验（字段英文名 + 物理类型归一化为硬指标，中文注释包含为次级指标）."""
        code = (std_code or "").strip()
        if not code:
            return {
                "status": "UNBOUND",
                "label": "未绑标",
                "detail": "尚未绑定数据标准",
                "target_type": "",
                "name_ok": False,
                "type_ok": False,
                "comment_ok": False,
            }
        if not std_item:
            return {
                "status": "STD_MISSING",
                "label": "标准缺失",
                "detail": f"标准字典中未找到编号 [{code}]",
                "target_type": "",
                "name_ok": False,
                "type_ok": False,
                "comment_ok": False,
            }

        target_type = cls.resolve_physical_type(
            std_item, db_type=db_type, format_profile=format_profile
        )
        std_en = clean_identifier_input(std_item.get("en_abbr") or "")
        actual_en = clean_identifier_input(col_name or "")
        name_ok = bool(std_en) and actual_en.lower() == std_en.lower()

        type_exact, type_compat = is_cross_type_compatible(col_type or "", target_type)
        type_ok = bool(type_exact)

        std_cn = (std_item.get("cn_name") or "").strip()
        actual_comm = (col_comment or "").strip()
        comment_ok = (not std_cn) or (std_cn in actual_comm)

        diffs: List[str] = []
        if not name_ok:
            diffs.append(f"字段名不符 ({col_name} → {std_en})")
        if not type_ok:
            if type_compat:
                diffs.append(f"类型精度差异 ({col_type or '?'} → {target_type})")
            else:
                diffs.append(f"类型不符 ({col_type or '?'} → {target_type})")
        if not comment_ok:
            diffs.append(f"注释缺失标准名「{std_cn}」")

        if name_ok and type_ok and comment_ok:
            return {
                "status": "COMPLIANT",
                "label": "已落标",
                "detail": "字段名、物理类型与注释均符合数据标准",
                "target_type": target_type,
                "name_ok": True,
                "type_ok": True,
                "comment_ok": True,
            }
        if name_ok and type_ok and not comment_ok:
            return {
                "status": "COMMENT_ONLY",
                "label": "仅差注释",
                "detail": "；".join(diffs),
                "target_type": target_type,
                "name_ok": True,
                "type_ok": True,
                "comment_ok": False,
            }
        return {
            "status": "NON_COMPLIANT",
            "label": "未落标",
            "detail": "；".join(diffs),
            "target_type": target_type,
            "name_ok": name_ok,
            "type_ok": type_ok,
            "comment_ok": comment_ok,
        }

    # =========================================================================
    # 数据源字段级数标绑定持久化 (field_standard_bindings)
    # =========================================================================

    @staticmethod
    def make_col_key(schema_name: str, table_name: str, column_name: str) -> str:
        s = clean_identifier_input(schema_name or "public").lower() or "public"
        t = clean_identifier_input(table_name or "").lower()
        c = clean_identifier_input(column_name or "").lower()
        return f"{s}::{t}::{c}"

    def get_table_field_bindings(
        self, connection_id: str, schema_name: str, table_name: str
    ) -> Dict[str, Dict[str, Any]]:
        """返回某表按 lowercase column_name 索引的绑定记录."""
        if not connection_id or not table_name:
            return {}
        sch = clean_identifier_input(schema_name or "public").lower() or "public"
        tbl = clean_identifier_input(table_name).lower()
        prefix = f"{sch}::{tbl}::"
        with self.store.module_db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM field_standard_bindings
                WHERE connection_id = ? AND col_key LIKE ?
                ORDER BY id ASC
                """,
                (connection_id, f"{prefix}%"),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    """
                    SELECT * FROM field_standard_bindings
                    WHERE connection_id = ? AND LOWER(table_name) = ? AND LOWER(schema_name) = ?
                    ORDER BY id ASC
                    """,
                    (connection_id, tbl, sch),
                ).fetchall()
        res: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            ck = clean_identifier_input(d["column_name"]).lower()
            res[ck] = d
        return res

    def upsert_field_bindings(
        self,
        connection_id: str,
        bindings: List[Dict[str, Any]],
        *,
        bind_source: str = "MANUAL",
    ) -> int:
        if not connection_id or not bindings:
            return 0
        now = self._now_str()
        changed = 0
        with self.store.module_db() as conn:
            for item in bindings:
                sch = clean_identifier_input(item.get("schema_name") or "public") or "public"
                tbl = clean_identifier_input(item.get("table_name") or "")
                col = clean_identifier_input(item.get("column_name") or "")
                if not tbl or not col:
                    continue
                col_key = self.make_col_key(sch, tbl, col)
                std_code = _clean_cell(item.get("std_code"))
                if not std_code:
                    cur = conn.execute(
                        "DELETE FROM field_standard_bindings WHERE connection_id = ? AND col_key = ?",
                        (connection_id, col_key),
                    )
                    changed += cur.rowcount
                    continue
                src = str(item.get("bind_source") or bind_source or "MANUAL").strip()
                c_status = str(item.get("compliance_status") or "UNCHECKED").strip()
                c_detail = str(item.get("compliance_detail") or "").strip()
                conn.execute(
                    """
                    INSERT INTO field_standard_bindings (
                        connection_id, schema_name, table_name, column_name, col_key,
                        std_code, bind_source, compliance_status, compliance_detail, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(connection_id, col_key) DO UPDATE SET
                        schema_name = excluded.schema_name,
                        table_name = excluded.table_name,
                        column_name = excluded.column_name,
                        std_code = excluded.std_code,
                        bind_source = excluded.bind_source,
                        compliance_status = excluded.compliance_status,
                        compliance_detail = excluded.compliance_detail,
                        updated_at = excluded.updated_at
                    """,
                    (
                        connection_id,
                        sch,
                        tbl,
                        col,
                        col_key,
                        std_code,
                        src,
                        c_status,
                        c_detail,
                        now,
                    ),
                )
                changed += 1
        return changed

    def replace_table_field_bindings(
        self,
        connection_id: str,
        schema_name: str,
        table_name: str,
        bindings: List[Dict[str, Any]],
        *,
        bind_source: str = "EXCEL",
    ) -> int:
        if not connection_id or not table_name:
            return 0
        sch = clean_identifier_input(schema_name or "public").lower() or "public"
        tbl = clean_identifier_input(table_name).lower()
        prefix = f"{sch}::{tbl}::"
        with self.store.module_db() as conn:
            conn.execute(
                "DELETE FROM field_standard_bindings WHERE connection_id = ? AND col_key LIKE ?",
                (connection_id, f"{prefix}%"),
            )
        return self.upsert_field_bindings(connection_id, bindings, bind_source=bind_source)

    def update_binding_compliance_batch(
        self, connection_id: str, updates: List[Dict[str, Any]]
    ) -> None:
        if not connection_id or not updates:
            return
        now = self._now_str()
        with self.store.module_db() as conn:
            conn.executemany(
                """
                UPDATE field_standard_bindings
                SET compliance_status = ?, compliance_detail = ?, updated_at = ?
                WHERE connection_id = ? AND col_key = ?
                """,
                [
                    (
                        str(u["compliance_status"]),
                        str(u.get("compliance_detail") or ""),
                        now,
                        connection_id,
                        self.make_col_key(u["schema_name"], u["table_name"], u["column_name"]),
                    )
                    for u in updates
                ],
            )

    def get_connection_binding_stats(self, connection_id: str) -> Dict[str, Dict[str, int]]:
        """返回指定数据源下按 lowercase `schema.table` 聚合的绑标/落标计数."""
        if not connection_id:
            return {}
        with self.store.module_db() as conn:
            rows = conn.execute(
                """
                SELECT
                    LOWER(schema_name) || '.' || LOWER(table_name) AS tbl_key,
                    COUNT(*) AS bound_count,
                    SUM(CASE WHEN compliance_status = 'COMPLIANT' THEN 1 ELSE 0 END) AS compliant_count,
                    SUM(CASE WHEN compliance_status = 'COMMENT_ONLY' THEN 1 ELSE 0 END) AS comment_only_count,
                    SUM(CASE WHEN compliance_status IN ('NON_COMPLIANT', 'STD_MISSING') THEN 1 ELSE 0 END) AS non_compliant_count
                FROM field_standard_bindings
                WHERE connection_id = ? AND std_code != ''
                GROUP BY LOWER(schema_name), LOWER(table_name)
                """,
                (connection_id,),
            ).fetchall()
        return {
            r["tbl_key"]: {
                "bound_count": int(r["bound_count"] or 0),
                "compliant_count": int(r["compliant_count"] or 0),
                "comment_only_count": int(r["comment_only_count"] or 0),
                "non_compliant_count": int(r["non_compliant_count"] or 0),
            }
            for r in rows
        }

    def list_bound_tables_for_connection(self, connection_id: str) -> List[Tuple[str, str]]:
        if not connection_id:
            return []
        with self.store.module_db() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT schema_name, table_name
                FROM field_standard_bindings
                WHERE connection_id = ? AND std_code != ''
                ORDER BY schema_name ASC, table_name ASC
                """,
                (connection_id,),
            ).fetchall()
        return [(r["schema_name"], r["table_name"]) for r in rows]

    def match_standard_candidates(
        self,
        *,
        col_name: str = "",
        col_comment: str = "",
        preferred_code: str = "",
    ) -> List[Dict[str, Any]]:
        """按编号 > 英文简称+中文名 > 英文简称 > 中文名在标准字典中智能匹配候选标准."""
        code = _clean_cell(preferred_code)
        if code:
            exact = self.get_item(code)
            if exact:
                return [exact]

        en = clean_identifier_input(col_name or "")
        cn = _clean_cell(col_comment)
        with self.store.module_db() as conn:
            if en and cn:
                rows = conn.execute(
                    """
                    SELECT * FROM standard_dictionary
                    WHERE LOWER(en_abbr) = LOWER(?) AND (cn_name = ? OR cn_name LIKE ?)
                    ORDER BY CASE WHEN is_standard_dict = '1' THEN 0 ELSE 1 END, std_code ASC
                    LIMIT 5
                    """,
                    (en, cn, f"%{cn}%"),
                ).fetchall()
                if rows:
                    return [self._row_to_item(r) for r in rows]

            if en:
                rows = conn.execute(
                    """
                    SELECT * FROM standard_dictionary
                    WHERE LOWER(en_abbr) = LOWER(?)
                    ORDER BY CASE WHEN is_standard_dict = '1' THEN 0 ELSE 1 END, std_code ASC
                    LIMIT 5
                    """,
                    (en,),
                ).fetchall()
                if rows:
                    return [self._row_to_item(r) for r in rows]

            if cn:
                rows = conn.execute(
                    """
                    SELECT * FROM standard_dictionary
                    WHERE cn_name = ? OR cn_alias = ?
                    ORDER BY CASE WHEN is_standard_dict = '1' THEN 0 ELSE 1 END, std_code ASC
                    LIMIT 5
                    """,
                    (cn, cn),
                ).fetchall()
                if rows:
                    return [self._row_to_item(r) for r in rows]
        return []
