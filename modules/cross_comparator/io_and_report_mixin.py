"""跨环境比对模块 — 铺底模板/上传、绑定包导入导出与库级离线报告混入类."""

from __future__ import annotations

import concurrent.futures
import io
import json
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.platform.dialects import (
    analyze_cross_column_compatibility,
    clean_identifier_input,
)
from core.platform.excel_io import (
    ResolvedWorkbook,
    clean_excel_cell_str,
    parse_excel_checklist,
    sanitize_excel_row,
    validate_xlsx_bytes,
)
from core.platform.import_progress import ProgressCallback, noop_progress
from core.platform.offline_report import (
    build_cross_env_excel_bytes,
    build_cross_env_html_bytes,
)


class _CrossIoAndReportMixin:
    """映射铺底 Excel 导入导出、标准包同步与库级跨环境 HTML/Excel 报告."""

    def generate_pairing_template_bytes(
        self,
        *,
        std_table_key: str = "",
        source_schema: str = "",
        source_table: str = "",
        target_schema: str = "",
        target_table: str = "",
    ) -> bytes:
        """生成跨环境字段映射铺底模板（支持空白模板与当前对比表预填字段模板）."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "跨环境字段映射"
        headers = [
            "来源模式名",
            "来源表名",
            "来源字段名",
            "来源数据项编号(选填)",
            "目标模式名",
            "目标表名",
            "目标字段名",
            "目标数据项编号(选填)",
        ]
        ws.append(headers)

        prefilled: List[List[str]] = []
        if std_table_key or source_table or target_table:
            active_key = (std_table_key or "").strip().lower()
            lookup_key = active_key
            load_saved_binding = bool(active_key)
            if not lookup_key:
                tables = self.ctx.store.list_cross_tables()
                matched_key = self._find_matching_table_key(
                    tables,
                    source_schema=source_schema,
                    source_table=source_table,
                    target_schema=target_schema,
                    target_table=target_table,
                )
                if matched_key:
                    lookup_key = matched_key
                    load_saved_binding = True
                else:
                    raw_fb = (target_table or source_table or "").strip()
                    if "." in raw_fb:
                        raw_fb = raw_fb.split(".", 1)[1]
                    lookup_key = clean_identifier_input(raw_fb).lower()
                    load_saved_binding = False
            comp = self.compare_single_table(
                lookup_key,
                override_source_schema=source_schema or None,
                override_source_table=source_table or None,
                override_target_schema=target_schema or None,
                override_target_table=target_table or None,
                persist_missing_binding=False,
                load_saved_binding=load_saved_binding,
            )
            tb = comp.get("table_binding") or {}
            s_sch = (
                tb.get("source_schema")
                or source_schema
                or (comp.get("source_panel") or {}).get("schema_name")
                or "public"
            )
            s_tbl = tb.get("source_table") or source_table or ""
            t_sch = (
                tb.get("target_schema")
                or target_schema
                or (comp.get("target_panel") or {}).get("schema_name")
                or "public"
            )
            t_tbl = tb.get("target_table") or target_table or ""
            for dr in comp.get("dual_rows") or []:
                sc = dr.get("source_col") or {}
                tc = dr.get("target_col") or {}
                prefilled.append(
                    [
                        s_sch,
                        s_tbl,
                        sc.get("column_name", ""),
                        dr.get("source_std_code", ""),
                        t_sch,
                        t_tbl,
                        tc.get("column_name", ""),
                        dr.get("target_std_code", ""),
                    ]
                )

        if prefilled:
            for r in prefilled:
                ws.append(sanitize_excel_row(r))
        else:
            for r in (
                ["ods", "ods_cust_info", "cust_no", "CUST000101", "dwd", "dwd_cust_info", "cust_id", "CUST000101"],
                ["ods", "ods_cust_info", "open_date", "ACCT000205", "dwd", "dwd_cust_info", "open_dt", "ACCT000205"],
            ):
                ws.append(r)

        req_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        opt_fill = PatternFill(start_color="334155", end_color="334155", fill_type="solid")
        header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
        for c_i in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=c_i)
            cell.fill = req_fill if c_i in (2, 3, 6, 7) else opt_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[get_column_letter(c_i)].width = 22
        ws.freeze_panes = "A2"

        ws_note = wb.create_sheet(title="填写说明")
        ws_note.append(["列名称", "是否必填", "填写说明"])
        for n in (
            ["来源模式名 / 来源表名 / 来源字段名", "必填", "来源库对应的 Schema、表名（或视图名）与字段名"],
            ["目标模式名 / 目标表名 / 目标字段名", "必填", "目标库对应的 Schema、表名与字段名"],
            ["来源/目标数据项编号", "选填", "填写第 1 步「标准字典」中的数据项编号；若两库在「落标绑定」中已绑定数标，可留空自动继承，也可在此处直接指定或覆盖"],
        ):
            ws_note.append(n)
        for c_i in range(1, 4):
            ws_note.cell(row=1, column=c_i).fill = req_fill
            ws_note.cell(row=1, column=c_i).font = header_font
        ws_note.column_dimensions["A"].width = 28
        ws_note.column_dimensions["B"].width = 12
        ws_note.column_dimensions["C"].width = 68

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def upload_pairings_excel(
        self,
        *,
        filename: str,
        content: bytes,
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        """批量上传跨环境对应关系铺底表（自动提取公式最终值、识别 #N/A 为空）."""
        report = progress_cb or noop_progress
        report(15.0, "校验与读取工作簿", "正在读取跨环境映射表（提取公式计算结果并过滤 #N/A）...", 0, 0)
        validate_xlsx_bytes(filename, content)
        self.ctx.standard_catalog.purge_invalid_error_bindings()

        def _norm(v: Any) -> str:
            return (
                clean_excel_cell_str(v)
                .lower()
                .replace("（", "(")
                .replace("）", ")")
                .replace(" ", "")
                .replace("_", "")
            )

        aliases = {
            "source_schema": {"来源模式名", "来源库模式", "源模式", "sourceschema"},
            "source_table": {"来源表名", "来源库表名", "源表名", "sourcetable"},
            "source_col": {"来源字段名", "来源库绑定字段", "源字段名", "sourcecol", "sourcecolumn"},
            "source_std": {"来源数据项编号(选填)", "来源数据项编号", "来源标准编号", "sourcestdcode"},
            "target_schema": {"目标模式名", "目标库模式", "targetschema"},
            "target_table": {"目标表名", "目标库表名", "targettable"},
            "target_col": {"目标字段名", "目标库绑定字段", "targetcol", "targetcolumn"},
            "target_std": {"目标数据项编号(选填)", "目标数据项编号", "目标标准编号", "targetstdcode"},
        }

        matched_sheets: List[Tuple[int, Dict[str, int], List[List[Any]]]] = []
        has_any_rows = False
        with ResolvedWorkbook(content, read_only_preferred=True) as rwb:
            for sh_title in rwb.sheetnames:
                t_norm = (sh_title or "").strip().lower()
                if t_norm in ("填写说明", "填表说明", "使用说明", "说明", "目录", "封面", "readme", "instructions"):
                    continue
                probe = list(rwb.iter_sheet_rows(sh_title, max_rows=15))
                if not probe:
                    continue
                has_any_rows = True
                found_h = -1
                found_map: Dict[str, int] = {}
                for r_i, row in enumerate(probe):
                    normed = [_norm(c) for c in row]
                    cand: Dict[str, int] = {}
                    for k, al_set in aliases.items():
                        for c_i, txt in enumerate(normed):
                            if txt in al_set and k not in cand:
                                cand[k] = c_i
                                break
                    if ("source_col" in cand or "target_col" in cand) and (
                        "source_table" in cand or "target_table" in cand
                    ):
                        found_h = r_i
                        found_map = cand
                        break
                if found_h != -1:
                    full_rows = rwb.extract_sheet_rows_with_merged(
                        sh_title,
                        row_progress_cb=lambda done_r, tot_r, _st=sh_title: report(
                            min(36.0, 17.0 + 19.0 * done_r / max(1, tot_r)),
                            "读取工作表并计算公式最终值",
                            f"正在解析 [{_st}] 第 {done_r} / {max(done_r, tot_r)} 行...",
                            done_r,
                            max(done_r, tot_r),
                        ),
                    )
                    if len(full_rows) >= found_h + 2:
                        matched_sheets.append((found_h, found_map, full_rows))

        if not has_any_rows:
            raise ValueError("映射铺底表为空，请下载系统模板填写后再上传")
        if not matched_sheets:
            raise ValueError("未识别到跨环境映射模板表头，请点击「下载模板」查看标准列名")

        grouped: Dict[str, Dict[str, Any]] = {}
        total_rows = 0
        total_scan_target = sum(max(0, len(sr) - (hi + 1)) for hi, _, sr in matched_sheets)
        scanned = 0

        for h_idx, cmap, rows in matched_sheets:
            for row in rows[h_idx + 1 :]:
                scanned += 1
                if scanned == 1 or scanned % 50 == 0 or scanned == total_scan_target:
                    pct = 36.0 + (22.0 * scanned / max(1, total_scan_target))
                    report(
                        pct,
                        "解析跨环境字段映射",
                        f"已扫描 {scanned} / {total_scan_target} 行，解析有效映射 {total_rows} 条...",
                        scanned,
                        total_scan_target,
                    )
                if not row or all(clean_excel_cell_str(x) == "" for x in row):
                    continue

                def _cell(k: str) -> str:
                    ci = cmap.get(k)
                    if ci is None or ci >= len(row):
                        return ""
                    return clean_excel_cell_str(row[ci], treat_placeholder_empty=True)

                raw_s_sch = clean_identifier_input(_cell("source_schema"))
                s_tbl = clean_identifier_input(_cell("source_table"))
                s_col = clean_identifier_input(_cell("source_col"))
                s_std = _cell("source_std")

                raw_t_sch = clean_identifier_input(_cell("target_schema"))
                t_tbl = clean_identifier_input(_cell("target_table"))
                t_col = clean_identifier_input(_cell("target_col"))
                t_std = _cell("target_std")

                if "." in s_tbl and not raw_s_sch:
                    ps, pt = s_tbl.split(".", 1)
                    raw_s_sch = clean_identifier_input(ps)
                    s_tbl = clean_identifier_input(pt)
                if "." in t_tbl and not raw_t_sch:
                    ps, pt = t_tbl.split(".", 1)
                    raw_t_sch = clean_identifier_input(ps)
                    t_tbl = clean_identifier_input(pt)

                t_key = clean_identifier_input(t_tbl or s_tbl).lower()
                if not t_key or (not s_col and not t_col):
                    continue

                grp = grouped.setdefault(
                    t_key,
                    {
                        "std_table_key": t_key,
                        "source_schema": raw_s_sch if s_tbl else "",
                        "source_table": s_tbl,
                        "target_schema": raw_t_sch if t_tbl else "",
                        "target_table": t_tbl,
                        "pairings": [],
                    },
                )
                if s_tbl and not grp["source_table"]:
                    grp["source_table"] = s_tbl
                if raw_s_sch and (s_tbl or not grp["source_schema"]):
                    if not grp["source_schema"]:
                        grp["source_schema"] = raw_s_sch
                if t_tbl and not grp["target_table"]:
                    grp["target_table"] = t_tbl
                if raw_t_sch and (t_tbl or not grp["target_schema"]):
                    if not grp["target_schema"]:
                        grp["target_schema"] = raw_t_sch

                p_item: Dict[str, Any] = {
                    "source_col": s_col,
                    "target_col": t_col,
                }
                if s_std:
                    p_item["source_std_code"] = s_std
                if t_std:
                    p_item["target_std_code"] = t_std
                grp["pairings"].append(p_item)
                total_rows += 1

        if not grouped:
            raise ValueError("未能从上传表中解析出有效映射关系行")

        src_cid = str(self.ctx.store.get_meta("cross_source_connection_id", "") or "")
        tgt_cid = str(self.ctx.store.get_meta("cross_target_connection_id", "") or "")
        src_prof = self.ctx.connection_hub.get_profile(src_cid) if src_cid else None
        tgt_prof = self.ctx.connection_hub.get_profile(tgt_cid) if tgt_cid else None
        src_def_sch = src_prof.effective_default_schema() if src_prof else "public"
        tgt_def_sch = tgt_prof.effective_default_schema() if tgt_prof else "public"
        tgt_db_type = tgt_prof.db_type if tgt_prof else "DWS"
        eff_tgt_fmt = self._resolve_side_format_profile(
            {"standard_format": getattr(tgt_prof, "standard_format", "AUTO")} if tgt_prof else {},
            target_format_profile,
            format_profile,
        )
        all_codes: List[str] = []
        for grp in grouped.values():
            for p in grp["pairings"]:
                sc_c = clean_excel_cell_str(p.get("source_std_code"), treat_placeholder_empty=True)
                tc_c = clean_excel_cell_str(p.get("target_std_code"), treat_placeholder_empty=True)
                if sc_c:
                    all_codes.append(sc_c)
                if tc_c:
                    all_codes.append(tc_c)
        std_map = self.ctx.standard_catalog.get_items_by_codes(all_codes)

        now = self.ctx.store._now_str()
        tbl_upsert_rows: List[Tuple[Any, ...]] = []
        del_tbl_keys: List[Tuple[str]] = []
        col_rows_all: List[Tuple[Any, ...]] = []
        src_sync_batch: List[Dict[str, Any]] = []
        tgt_sync_batch: List[Dict[str, Any]] = []
        last_key = ""

        for t_key, grp in grouped.items():
            last_key = t_key
            s_sch = (grp["source_schema"] or "").strip() or src_def_sch
            s_tbl = (grp["source_table"] or "").strip() or (grp["target_table"] or "").strip() or t_key
            t_sch = (grp["target_schema"] or "").strip() or tgt_def_sch
            t_tbl = (grp["target_table"] or "").strip() or (grp["source_table"] or "").strip() or t_key
            std_tbl_name = clean_identifier_input(t_tbl or s_tbl or t_key) or t_key

            tbl_upsert_rows.append(
                (
                    t_key,
                    t_sch or s_sch,
                    std_tbl_name,
                    "",
                    s_sch,
                    s_tbl,
                    t_sch,
                    t_tbl,
                    now,
                )
            )
            del_tbl_keys.append((t_key,))

            for idx, p in enumerate(grp["pairings"], start=1):
                s_col = clean_identifier_input(
                    clean_excel_cell_str(p.get("source_col"), treat_placeholder_empty=True)
                )
                t_col = clean_identifier_input(
                    clean_excel_cell_str(p.get("target_col"), treat_placeholder_empty=True)
                )
                s_code = clean_excel_cell_str(p.get("source_std_code"), treat_placeholder_empty=True)
                t_code = clean_excel_cell_str(p.get("target_std_code"), treat_placeholder_empty=True)
                if not s_col and not t_col:
                    continue

                if s_col and t_col:
                    rep_item = std_map.get(t_code) or std_map.get(s_code)
                    std_col_name = (rep_item.get("en_abbr") if rep_item else "") or t_col or s_col
                    std_col_type = (
                        self.ctx.standard_catalog.resolve_physical_type(
                            rep_item, db_type=tgt_db_type, format_profile=eff_tgt_fmt
                        )
                        if rep_item
                        else ""
                    )
                    std_col_comm = rep_item.get("cn_name", "") if rep_item else ""
                    orig_col_name = s_col
                elif s_col:
                    std_col_name = f"__unpaired_src__::{s_col}"
                    std_col_type = ""
                    std_col_comm = ""
                    orig_col_name = s_col
                else:
                    std_col_name = f"__unpaired_tgt__::{t_col}"
                    std_col_type = ""
                    std_col_comm = ""
                    orig_col_name = t_col

                col_rows_all.append(
                    (
                        t_key,
                        idx,
                        orig_col_name,
                        "",
                        "",
                        std_col_name,
                        std_col_type,
                        std_col_comm,
                        s_col,
                        t_col,
                        s_code,
                        t_code,
                        now,
                    )
                )
                if src_cid and s_col and s_tbl and "source_std_code" in p:
                    src_sync_batch.append(
                        {
                            "schema_name": s_sch,
                            "table_name": s_tbl,
                            "column_name": s_col,
                            "std_code": s_code,
                        }
                    )
                if tgt_cid and t_col and t_tbl and "target_std_code" in p:
                    tgt_sync_batch.append(
                        {
                            "schema_name": t_sch,
                            "table_name": t_tbl,
                            "column_name": t_col,
                            "std_code": t_code,
                        }
                    )

        report(
            62.0,
            "保存双列映射与同步标准绑定",
            f"正在批量持久化 {len(grouped)} 张表共 {len(col_rows_all)} 条字段映射...",
            0,
            max(1, len(col_rows_all)),
        )
        chunk_size = 500
        with self.ctx.store.module_db() as conn:
            conn.executemany(
                """
                INSERT INTO cross_table_bindings (
                    std_table_key, std_schema_name, std_table_name, std_table_comment,
                    source_schema, source_table, target_schema, target_table, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(std_table_key) DO UPDATE SET
                    source_schema = excluded.source_schema,
                    source_table = excluded.source_table,
                    target_schema = excluded.target_schema,
                    target_table = excluded.target_table,
                    updated_at = excluded.updated_at
                """,
                tbl_upsert_rows,
            )
            conn.executemany("DELETE FROM cross_column_bindings WHERE std_table_key = ?", del_tbl_keys)
            for start_i in range(0, len(col_rows_all), chunk_size):
                batch_c = col_rows_all[start_i : start_i + chunk_size]
                conn.executemany(
                    """
                    INSERT INTO cross_column_bindings (
                        std_table_key, row_index,
                        orig_col_name, orig_col_type, orig_col_comment,
                        std_col_name, std_col_type, std_col_comment,
                        bound_source_col, bound_target_col,
                        source_std_code, target_std_code, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch_c,
                )
                done_c = min(len(col_rows_all), start_i + len(batch_c))
                pct = 62.0 + 20.0 * (done_c / max(1, len(col_rows_all)))
                report(
                    pct,
                    "保存双列映射与同步标准绑定",
                    f"已写入跨环境映射 {done_c} / {len(col_rows_all)} 条（共 {len(grouped)} 张表）...",
                    done_c,
                    len(col_rows_all),
                )

        if src_sync_batch:
            self.ctx.standard_catalog.bulk_save_field_bindings(
                src_cid,
                src_sync_batch,
                mode="merge",
                bind_source="CROSS_SYNC",
                progress_cb=report,
                progress_start_pct=82.0,
                progress_end_pct=88.0,
            )
        if tgt_sync_batch:
            self.ctx.standard_catalog.bulk_save_field_bindings(
                tgt_cid,
                tgt_sync_batch,
                mode="merge",
                bind_source="CROSS_SYNC",
                progress_cb=report,
                progress_start_pct=88.0,
                progress_end_pct=94.0,
            )

        self.ctx.store.add_log(
            "INFO",
            "跨环境映射批量铺底",
            f"已从 [{filename}] 批量保存 {len(grouped)} 张表共 {len(col_rows_all)} 行双列映射与数标绑定",
        )
        report(96.0, "加载当前对比视图", f"正在读取表 [{last_key}] 对照预览...", total_rows, total_rows)
        state = self.get_overview_state(
            selected_table_key=last_key,
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
        )
        state["imported_count"] = total_rows
        state["message"] = f"已从 [{filename}] 铺底导入 {len(grouped)} 张表共 {total_rows} 条字段映射关系"
        return state

    def import_standard_or_bundle(
        self,
        filename: str,
        content: bytes,
        *,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        lower_fn = (filename or "").strip().lower()
        if lower_fn.endswith(".json"):
            report(30.0, "解析绑定配置包", "正在读取 JSON 绑定包...", 0, 0)
            bundle = json.loads(content.decode("utf-8"))
            res = self.ctx.store.import_cross_bindings_bundle(bundle, filename=filename)
            self.ctx.store.add_log(
                "SUCCESS",
                "数标导入",
                f"成功从 JSON 绑定包 [{filename}] 恢复 {res['table_count']} 张表、{res['column_count']} 条字段绑定关系",
            )
            return self.get_overview_state()

        report(20.0, "校验与读取工作簿", "正在解析跨环境标准表（自动提取公式结果与过滤 #N/A）...", 0, 0)
        validate_xlsx_bytes(filename, content)

        bundle_rows: List[List[Any]] = []
        with ResolvedWorkbook(content, read_only_preferred=True) as rwb:
            first_sheet = rwb.sheetnames[0] if rwb.sheetnames else ""
            probe = list(rwb.iter_sheet_rows(first_sheet, max_rows=2)) if first_sheet else []
            if len(probe) >= 2:
                probe_headers = [clean_excel_cell_str(c) for c in probe[0]]
                if "数据标准字段名" in probe_headers and "来源库绑定字段" in probe_headers:
                    bundle_rows = list(
                        rwb.iter_sheet_rows(
                            first_sheet,
                            row_progress_cb=lambda done_r, tot_r: report(
                                min(46.0, 22.0 + 24.0 * done_r / max(1, tot_r)),
                                "读取跨环境标准与绑定表",
                                f"正在读取 [{first_sheet}] 第 {done_r} / {max(done_r, tot_r)} 行...",
                                done_r,
                                max(done_r, tot_r),
                            ),
                        )
                    )

        if len(bundle_rows) >= 2:
            header_cells = [clean_excel_cell_str(c) for c in bundle_rows[0]]
            idx_map = {h: i for i, h in enumerate(header_cells)}
            items: List[Dict[str, Any]] = []
            data_rows = bundle_rows[1:]
            total_data = len(data_rows)

            def _rcell(r_vals: List[Any], h_name: str, fallback_idx: int, placeholder_empty: bool = False) -> str:
                c_i = idx_map.get(h_name, fallback_idx)
                if c_i >= len(r_vals):
                    return ""
                return clean_excel_cell_str(r_vals[c_i], treat_placeholder_empty=placeholder_empty)

            for scanned_idx, r in enumerate(data_rows, start=1):
                if r and not all(clean_excel_cell_str(x) == "" for x in r):
                    std_tbl = _rcell(r, "标准表名", 2, True)
                    std_col = _rcell(r, "数据标准字段名", 11, True)
                    if std_tbl and std_col:
                        items.append(
                            {
                                "schema_name": _rcell(r, "标准模式名", 1, True) or "public",
                                "table_name": std_tbl,
                                "source_schema": _rcell(r, "来源库模式", 3, True),
                                "source_table": _rcell(r, "来源库表名", 4, True) or std_tbl,
                                "bound_source_col": _rcell(r, "来源库绑定字段", 5, True),
                                "old_col_name": _rcell(r, "清单原字段名", 8, True),
                                "old_col_type": _rcell(r, "清单原类型", 9, True),
                                "old_col_comment": _rcell(r, "清单原注释", 10, False),
                                "new_col_name": std_col,
                                "new_col_type": _rcell(r, "数据标准类型", 12, True),
                                "new_col_comment": _rcell(r, "数据标准注释", 13, False),
                                "target_schema": _rcell(r, "目标库模式", 14, True),
                                "target_table": _rcell(r, "目标库表名", 15, True) or std_tbl,
                                "bound_target_col": _rcell(r, "目标库绑定字段", 16, True),
                            }
                        )
                if scanned_idx == 1 or scanned_idx % 50 == 0 or scanned_idx == total_data:
                    pct = 46.0 + (26.0 * scanned_idx / max(1, total_data))
                    report(
                        pct,
                        "解析跨环境标准映射",
                        f"已解析 {scanned_idx} / {total_data} 行，识别有效标准字段 {len(items)} 条...",
                        scanned_idx,
                        total_data,
                    )
            if items:
                report(75.0, "保存标准与映射", f"正在保存 {len(items)} 条跨环境标准字段...", len(items), len(items))
                self.ctx.store.replace_cross_standards(items, filename, preserve_bindings=False)
                return self.get_overview_state()

        parsed_items = parse_excel_checklist(
            content,
            filename,
            row_progress_cb=lambda done_r, tot_r: report(
                min(68.0, 24.0 + 44.0 * done_r / max(1, tot_r)),
                "解析跨环境标准清单与公式结果",
                f"正在读取工作表并计算公式结果（已处理 {done_r} / {max(done_r, tot_r)} 行）...",
                done_r,
                max(done_r, tot_r),
            ),
        )
        report(75.0, "保存标准清单", f"正在写入 {len(parsed_items)} 条标准规则...", len(parsed_items), len(parsed_items))
        self.ctx.store.replace_cross_standards(parsed_items, filename, preserve_bindings=True)
        return self.get_overview_state()

    def export_comparison_excel_bytes(self, std_table_key: Optional[str] = None) -> bytes:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "跨环境三方数标比对与绑定表"

        headers = [
            "序号",
            "标准模式名",
            "标准表名",
            "来源库模式",
            "来源库表名",
            "来源库绑定字段",
            "来源库当前类型",
            "来源库当前注释",
            "清单原字段名",
            "清单原类型",
            "清单原注释",
            "数据标准字段名",
            "数据标准类型",
            "数据标准注释",
            "目标库模式",
            "目标库表名",
            "目标库绑定字段",
            "目标库当前类型",
            "目标库当前注释",
            "目标库落标状态",
            "三方比对诊断详情",
            "目标长度/精度包络",
            "是否涉及类型转换",
        ]
        ws.append(headers)

        header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True, size=10)
        thin_border = Border(
            left=Side(style="thin", color="CBD5E1"),
            right=Side(style="thin", color="CBD5E1"),
            top=Side(style="thin", color="CBD5E1"),
            bottom=Side(style="thin", color="CBD5E1"),
        )
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border

        tables = (
            [self.ctx.store.get_cross_table_binding(std_table_key)]
            if std_table_key
            else self.ctx.store.list_cross_tables()
        )
        tables = [t for t in tables if t]

        src_conn_brief = self.get_role_connection_brief("source")
        tgt_conn_brief = self.get_role_connection_brief("target")
        row_num = 1
        for t in tables:
            comp = self.compare_single_table(
                t["std_table_key"],
                src_conn=src_conn_brief,
                tgt_conn=tgt_conn_brief,
            )
            t_bind = comp["table_binding"]
            for sc in comp["standard_panel"]["columns"]:
                src_m = sc.get("source_col_meta") or {}
                tgt_m = sc.get("target_col_meta") or {}
                cc = sc.get("cross_compat") or {}
                ws.append(
                    sanitize_excel_row(
                        [
                            row_num,
                            t_bind.get("std_schema_name", "public"),
                            t_bind.get("std_table_name", ""),
                            t_bind.get("source_schema", ""),
                            t_bind.get("source_table", ""),
                            sc.get("effective_source_col", ""),
                            src_m.get("data_type", ""),
                            src_m.get("column_comment", ""),
                            sc.get("orig_col_name", ""),
                            sc.get("orig_col_type", ""),
                            sc.get("orig_col_comment", ""),
                            sc.get("std_col_name", ""),
                            sc.get("std_col_type", ""),
                            sc.get("std_col_comment", ""),
                            t_bind.get("target_schema", ""),
                            t_bind.get("target_table", ""),
                            sc.get("effective_target_col", ""),
                            tgt_m.get("data_type", ""),
                            tgt_m.get("column_comment", ""),
                            sc.get("target_status", ""),
                            sc.get("target_detail", ""),
                            cc.get("capacity_label") or "-",
                            cc.get("type_conv_label") or "-",
                        ]
                    )
                )
                row_num += 1

        widths = [6, 14, 22, 14, 22, 20, 18, 22, 20, 16, 20, 22, 18, 24, 14, 22, 20, 18, 22, 16, 36, 22, 22]
        for idx, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # =========================================================================
    # 库级跨环境离线报告 (单 Sheet .xlsx + 离线交互 .html + 推荐同名表清单)
    # =========================================================================

    @staticmethod
    def _hydrate_five_col_pair(comp: Dict[str, Any]) -> Dict[str, Any]:
        """将单表比对结果整理为离线网页与 1-Sheet Excel 复用的五虚拟列数据契约（与前端五栏口径严格一致）."""
        dual_rows = comp.get("dual_rows") or []
        src_panel_raw = comp.get("source_panel") or {}
        tgt_panel_raw = comp.get("target_panel") or {}

        center_rows: List[Dict[str, Any]] = []
        source_candidates: List[Dict[str, Any]] = []
        target_candidates: List[Dict[str, Any]] = []
        seen_src: Set[str] = set()
        seen_tgt: Set[str] = set()

        bk_summary = {
            "total_rows": 0,
            "paired_rows": 0,
            "unified": 0,
            "split_diff": 0,
            "split_partial": 0,
            "split_unbound": 0,
            "target_needs_migration": 0,
            "capacity_insufficient": 0,
            "capacity_risk": 0,
            "type_conversion": 0,
        }
        src_db_type = str(src_panel_raw.get("db_type") or "")
        tgt_db_type = str(tgt_panel_raw.get("db_type") or "")

        for dr in dual_rows:
            s_col = dr.get("source_col")
            t_col = dr.get("target_col")
            s_code = str(dr.get("source_std_code") or "").strip()
            t_code = str(dr.get("target_std_code") or "").strip()

            if s_col and s_col.get("column_name") and not s_code:
                k = str(s_col["column_name"]).lower()
                if k not in seen_src:
                    seen_src.add(k)
                    source_candidates.append(s_col)
            if t_col and t_col.get("column_name") and not t_code:
                k = str(t_col["column_name"]).lower()
                if k not in seen_tgt:
                    seen_tgt.add(k)
                    target_candidates.append(t_col)

            bound_src = s_col if (s_col and s_code) else None
            bound_tgt = t_col if (t_col and t_code) else None
            if bound_src and bound_src.get("column_name"):
                seen_src.add(str(bound_src["column_name"]).lower())
            if bound_tgt and bound_tgt.get("column_name"):
                seen_tgt.add(str(bound_tgt["column_name"]).lower())

            if not bound_src and not bound_tgt:
                continue

            paired = bool(bound_src and bound_tgt)
            if paired and s_code == t_code:
                bk_state = "UNIFIED"
                bk_summary["unified"] += 1
            elif s_code and t_code and s_code != t_code:
                bk_state = "SPLIT_DIFF"
                bk_summary["split_diff"] += 1
            else:
                bk_state = "SPLIT_PARTIAL"
                bk_summary["split_partial"] += 1

            if paired:
                bk_summary["paired_rows"] += 1
            bk_summary["total_rows"] += 1
            t_needs_mig = bool(bound_tgt and dr.get("target_needs_migration"))
            if t_needs_mig:
                bk_summary["target_needs_migration"] += 1

            if paired:
                raw_cc = dr.get("cross_compat")
                if isinstance(raw_cc, dict) and "capacity_status" in raw_cc:
                    cc = dict(raw_cc)
                else:
                    cc = analyze_cross_column_compatibility(
                        str((bound_src or {}).get("data_type") or ""),
                        str((bound_tgt or {}).get("data_type") or ""),
                        source_db_type=src_db_type,
                        target_db_type=tgt_db_type,
                    )
            else:
                cc = analyze_cross_column_compatibility("", "")

            if cc.get("applicable"):
                if cc.get("capacity_status") == "INSUFFICIENT":
                    bk_summary["capacity_insufficient"] += 1
                elif cc.get("capacity_status") == "RISK":
                    bk_summary["capacity_risk"] += 1
                if cc.get("has_type_conversion"):
                    bk_summary["type_conversion"] += 1

            center_rows.append(
                {
                    **dr,
                    "paired": paired,
                    "bookmark_state": bk_state,
                    "source_col": bound_src,
                    "target_col": bound_tgt,
                    "source_std_code": s_code if bound_src else "",
                    "target_std_code": t_code if bound_tgt else "",
                    "source_std": dr.get("source_std") if bound_src else None,
                    "target_std": dr.get("target_std") if bound_tgt else None,
                    "target_compliance": dr.get("target_compliance") if bound_tgt else None,
                    "target_needs_migration": t_needs_mig,
                    "cross_compat": cc,
                }
            )

        for c in src_panel_raw.get("columns") or []:
            k = str(c.get("column_name") or "").lower()
            if k and k not in seen_src:
                seen_src.add(k)
                source_candidates.append(c)
        for c in tgt_panel_raw.get("columns") or []:
            k = str(c.get("column_name") or "").lower()
            if k and k not in seen_tgt:
                seen_tgt.add(k)
                target_candidates.append(c)

        return {
            "table_binding": comp.get("table_binding") or {},
            "summary": comp.get("summary") or {},
            "bookmark_summary": bk_summary,
            "source_format_profile": comp.get("source_format_profile") or "AUTO",
            "target_format_profile": comp.get("target_format_profile") or "AUTO",
            "source_panel": {
                "configured": bool(src_panel_raw.get("configured")),
                "connection_id": src_panel_raw.get("connection_id", ""),
                "connection_name": src_panel_raw.get("connection_name", ""),
                "db_type": src_panel_raw.get("db_type", ""),
                "standard_format": src_panel_raw.get("standard_format", "AUTO"),
                "schema_name": src_panel_raw.get("schema_name", ""),
                "table_name": src_panel_raw.get("table_name", ""),
                "exists": bool(src_panel_raw.get("exists")),
                "object_type": src_panel_raw.get("object_type", "TABLE"),
                "table_comment": src_panel_raw.get("table_comment", ""),
                "error": src_panel_raw.get("error", ""),
            },
            "target_panel": {
                "configured": bool(tgt_panel_raw.get("configured")),
                "connection_id": tgt_panel_raw.get("connection_id", ""),
                "connection_name": tgt_panel_raw.get("connection_name", ""),
                "db_type": tgt_panel_raw.get("db_type", ""),
                "standard_format": tgt_panel_raw.get("standard_format", "AUTO"),
                "schema_name": tgt_panel_raw.get("schema_name", ""),
                "table_name": tgt_panel_raw.get("table_name", ""),
                "exists": bool(tgt_panel_raw.get("exists")),
                "object_type": tgt_panel_raw.get("object_type", "TABLE"),
                "table_comment": tgt_panel_raw.get("table_comment", ""),
                "error": tgt_panel_raw.get("error", ""),
            },
            "source_candidates": source_candidates,
            "center_rows": center_rows,
            "target_candidates": target_candidates,
        }

    _NORM_SCHEMA_DIALECTS = frozenset(
        {
            "SQLITE",
            "INCEPTOR",
            "MYSQL",
            "MARIADB",
            "OCEANBASE",
            "DORIS",
            "STARROCKS",
            "TIDB",
        }
    )

    @classmethod
    def _norm_side_schema(cls, raw_sch: str, def_sch: str, db_type: str) -> str:
        cs = clean_identifier_input(raw_sch or "") or def_sch
        dt = (db_type or "").strip().upper()
        if cs.lower() == "public" and def_sch.lower() != "public" and dt in cls._NORM_SCHEMA_DIALECTS:
            return def_sch
        if dt == "SQLITE":
            return "main"
        return cs

    @classmethod
    def _build_side_schema_filter(
        cls,
        schemas: Union[str, Sequence[str]],
        def_sch: str,
        db_type: str,
    ) -> Tuple[List[str], Set[str], Callable[[str], bool]]:
        raw_items = (
            [x.strip() for x in schemas.split(",") if x.strip()]
            if isinstance(schemas, str)
            else [str(x).strip() for x in (schemas or []) if str(x).strip()]
        )
        selected: List[str] = []
        seen_sel: Set[str] = set()
        for raw_s in raw_items:
            cs = clean_identifier_input(raw_s)
            if cs and cs.lower() not in seen_sel:
                seen_sel.add(cs.lower())
                selected.append(cs)

        sel_set: Set[str] = set()
        for s in selected:
            sel_set.add(s.lower())
            sel_set.add(cls._norm_side_schema(s, def_sch, db_type).lower())

        def _matches(raw_s: str) -> bool:
            if not sel_set:
                return True
            cs = clean_identifier_input(raw_s or "") or def_sch
            return (
                cs.lower() in sel_set
                or cls._norm_side_schema(cs, def_sch, db_type).lower() in sel_set
            )

        return selected, sel_set, _matches

    @classmethod
    def _compute_recommended_pairings(
        cls,
        *,
        saved_tables: List[Dict[str, Any]],
        src_cat_tables: List[Dict[str, Any]],
        tgt_cat_tables: List[Dict[str, Any]],
        src_def_schema: str,
        tgt_def_schema: str,
        src_bind_stats: Dict[str, Dict[str, int]],
        tgt_bind_stats: Dict[str, Dict[str, int]],
        src_db_type: str = "",
        tgt_db_type: str = "",
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """仅基于目录表名与 SQLite 绑标统计计算推荐同名表与单侧独有表（严禁执行自动列预比对）."""
        saved_src_keys: Set[Tuple[str, str]] = set()
        saved_tgt_keys: Set[Tuple[str, str]] = set()
        for t in saved_tables:
            s_s = cls._norm_side_schema(str(t.get("source_schema") or ""), src_def_schema, src_db_type)
            s_t = clean_identifier_input(str(t.get("source_table") or t.get("std_table_name") or ""))
            if s_t:
                saved_src_keys.add((s_s.lower(), s_t.lower()))
            t_s = cls._norm_side_schema(str(t.get("target_schema") or ""), tgt_def_schema, tgt_db_type)
            t_t = clean_identifier_input(str(t.get("target_table") or t.get("std_table_name") or ""))
            if t_t:
                saved_tgt_keys.add((t_s.lower(), t_t.lower()))

        def _extract_unpaired(
            cat_list: List[Dict[str, Any]],
            saved_keys: Set[Tuple[str, str]],
            def_schema: str,
            bind_stats: Dict[str, Dict[str, int]],
        ) -> List[Dict[str, Any]]:
            items: List[Dict[str, Any]] = []
            seen: Set[Tuple[str, str]] = set()
            for row in cat_list:
                sch = clean_identifier_input(str(row.get("schema_name") or def_schema)) or def_schema
                tbl = clean_identifier_input(str(row.get("table_name") or ""))
                if not tbl:
                    continue
                k = (sch.lower(), tbl.lower())
                if k in saved_keys or k in seen:
                    continue
                seen.add(k)
                stat = bind_stats.get(f"{k[0]}.{k[1]}") or {}
                items.append(
                    {
                        "schema_name": sch,
                        "table_name": tbl,
                        "object_type": str(row.get("object_type") or "TABLE"),
                        "table_comment": str(row.get("table_comment") or ""),
                        "bound_count": int(stat.get("bound_count") or 0),
                    }
                )
            return items

        src_unpaired = _extract_unpaired(
            src_cat_tables, saved_src_keys, src_def_schema, src_bind_stats
        )
        tgt_unpaired = _extract_unpaired(
            tgt_cat_tables, saved_tgt_keys, tgt_def_schema, tgt_bind_stats
        )

        tgt_by_exact: Dict[Tuple[str, str], List[int]] = {}
        tgt_by_name: Dict[str, List[int]] = {}
        for idx, t_item in enumerate(tgt_unpaired):
            s_l = t_item["schema_name"].lower()
            t_l = t_item["table_name"].lower()
            tgt_by_exact.setdefault((s_l, t_l), []).append(idx)
            tgt_by_name.setdefault(t_l, []).append(idx)

        matched_src_idx: Set[int] = set()
        matched_tgt_idx: Set[int] = set()
        recommendations: List[Dict[str, Any]] = []

        # Pass 1: 优先同模式同名精确推荐
        for s_idx, s_item in enumerate(src_unpaired):
            k_exact = (s_item["schema_name"].lower(), s_item["table_name"].lower())
            for t_idx in tgt_by_exact.get(k_exact, ()):
                if t_idx in matched_tgt_idx:
                    continue
                matched_src_idx.add(s_idx)
                matched_tgt_idx.add(t_idx)
                t_item = tgt_unpaired[t_idx]
                recommendations.append(
                    {
                        "table_name": s_item["table_name"],
                        "source_schema": s_item["schema_name"],
                        "source_table": s_item["table_name"],
                        "source_object_type": s_item["object_type"],
                        "source_comment": s_item["table_comment"],
                        "source_bound_count": s_item["bound_count"],
                        "target_schema": t_item["schema_name"],
                        "target_table": t_item["table_name"],
                        "target_object_type": t_item["object_type"],
                        "target_comment": t_item["table_comment"],
                        "target_bound_count": t_item["bound_count"],
                        "match_type": "同模式同名",
                    }
                )
                break

        # Pass 2: 跨模式同名推荐
        for s_idx, s_item in enumerate(src_unpaired):
            if s_idx in matched_src_idx:
                continue
            for t_idx in tgt_by_name.get(s_item["table_name"].lower(), ()):
                if t_idx in matched_tgt_idx:
                    continue
                matched_src_idx.add(s_idx)
                matched_tgt_idx.add(t_idx)
                t_item = tgt_unpaired[t_idx]
                recommendations.append(
                    {
                        "table_name": s_item["table_name"],
                        "source_schema": s_item["schema_name"],
                        "source_table": s_item["table_name"],
                        "source_object_type": s_item["object_type"],
                        "source_comment": s_item["table_comment"],
                        "source_bound_count": s_item["bound_count"],
                        "target_schema": t_item["schema_name"],
                        "target_table": t_item["table_name"],
                        "target_object_type": t_item["object_type"],
                        "target_comment": t_item["table_comment"],
                        "target_bound_count": t_item["bound_count"],
                        "match_type": "跨模式同名",
                    }
                )
                break

        source_only = [it for idx, it in enumerate(src_unpaired) if idx not in matched_src_idx]
        target_only = [it for idx, it in enumerate(tgt_unpaired) if idx not in matched_tgt_idx]
        return recommendations, source_only, target_only

    def build_database_cross_report_data(
        self,
        *,
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
        source_schemas: Union[str, Sequence[str]] = "",
        target_schemas: Union[str, Sequence[str]] = "",
        only_bound_tables: bool = True,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        """构建跨环境库级比对报告数据（自动沿用按库落标格式，支持两侧 Schema 筛选与表级已绑标范围收敛）."""
        report = progress_cb or noop_progress
        report(4.0, "加载配置与连接", "正在读取来源库、目标库及按库数据标准格式...", 0, 0)

        src_conn = self.get_role_connection_info("source")
        tgt_conn = self.get_role_connection_info("target")
        eff_src_fmt = self._resolve_side_format_profile(
            src_conn, source_format_profile, format_profile
        )
        eff_tgt_fmt = self._resolve_side_format_profile(
            tgt_conn, target_format_profile, format_profile
        )
        fmt_map = {
            p["value"]: p["label"]
            for p in self.ctx.standard_catalog.list_format_profiles()
        }

        src_cid = str(src_conn.get("connection_id") or "") if src_conn.get("configured") else ""
        tgt_cid = str(tgt_conn.get("connection_id") or "") if tgt_conn.get("configured") else ""
        same_cid = bool(src_cid and src_cid == tgt_cid)

        if src_cid:
            self.ctx.standard_catalog.purge_invalid_error_bindings(src_cid)
        if tgt_cid and not same_cid:
            self.ctx.standard_catalog.purge_invalid_error_bindings(tgt_cid)

        all_saved_tables, _, _, _ = self._resolve_comparison_target()

        if not src_cid and not tgt_cid and not all_saved_tables:
            raise ValueError("请先在跨环境比对中选择来源库或目标库，或先保存/导入映射表对")

        src_def_sch = str(
            src_conn.get("effective_default_schema") or src_conn.get("default_schema") or "public"
        )
        tgt_def_sch = str(
            tgt_conn.get("effective_default_schema") or tgt_conn.get("default_schema") or "public"
        )
        src_db_type = str(src_conn.get("db_type") or "")
        tgt_db_type = str(tgt_conn.get("db_type") or "")

        sel_src_schemas, src_sch_set, _src_schema_matches = self._build_side_schema_filter(
            source_schemas, src_def_sch, src_db_type
        )
        sel_tgt_schemas, tgt_sch_set, _tgt_schema_matches = self._build_side_schema_filter(
            target_schemas, tgt_def_sch, tgt_db_type
        )

        src_bound_tables = (
            self.ctx.standard_catalog.list_bound_tables_for_connection(src_cid) if src_cid else []
        )
        tgt_bound_tables = (
            src_bound_tables
            if same_cid
            else (
                self.ctx.standard_catalog.list_bound_tables_for_connection(tgt_cid)
                if tgt_cid
                else []
            )
        )

        def _build_bound_key_set(
            b_list: List[Tuple[str, str]], def_sch: str, db_type: str
        ) -> Set[Tuple[str, str]]:
            keys: Set[Tuple[str, str]] = set()
            for bs, bt in b_list:
                sc = clean_identifier_input(bs or def_sch) or def_sch
                tc = clean_identifier_input(bt or "")
                if not tc:
                    continue
                keys.add((sc.lower(), tc.lower()))
                keys.add((self._norm_side_schema(sc, def_sch, db_type).lower(), tc.lower()))
            return keys

        src_bound_keys = _build_bound_key_set(src_bound_tables, src_def_sch, src_db_type)
        tgt_bound_keys = _build_bound_key_set(tgt_bound_tables, tgt_def_sch, tgt_db_type)

        def _pair_matches_schemas(tb: Dict[str, Any]) -> bool:
            ss = clean_identifier_input(str(tb.get("source_schema") or src_def_sch)) or src_def_sch
            ts = clean_identifier_input(str(tb.get("target_schema") or tgt_def_sch)) or tgt_def_sch
            return (not src_sch_set or _src_schema_matches(ss)) and (
                not tgt_sch_set or _tgt_schema_matches(ts)
            )

        schema_saved_tables = [tb for tb in all_saved_tables if _pair_matches_schemas(tb)]

        def _pair_has_binding(tb: Dict[str, Any]) -> bool:
            if int(tb.get("bound_std_count") or 0) > 0:
                return True
            ss = clean_identifier_input(str(tb.get("source_schema") or src_def_sch)) or src_def_sch
            st = clean_identifier_input(str(tb.get("source_table") or tb.get("std_table_name") or ""))
            if st and (
                (ss.lower(), st.lower()) in src_bound_keys
                or (self._norm_side_schema(ss, src_def_sch, src_db_type).lower(), st.lower())
                in src_bound_keys
            ):
                return True
            ts = clean_identifier_input(str(tb.get("target_schema") or tgt_def_sch)) or tgt_def_sch
            tt = clean_identifier_input(str(tb.get("target_table") or tb.get("std_table_name") or ""))
            if tt and (
                (ts.lower(), tt.lower()) in tgt_bound_keys
                or (self._norm_side_schema(ts, tgt_def_sch, tgt_db_type).lower(), tt.lower())
                in tgt_bound_keys
            ):
                return True
            return False

        probe_saved_tables = (
            [tb for tb in schema_saved_tables if _pair_has_binding(tb)]
            if only_bound_tables
            else list(schema_saved_tables)
        )

        src_has_bound_in_scope = any(
            _src_schema_matches(clean_identifier_input(bs or src_def_sch) or src_def_sch)
            for bs, _bt in src_bound_tables
        )
        tgt_has_bound_in_scope = any(
            _tgt_schema_matches(clean_identifier_input(bs or tgt_def_sch) or tgt_def_sch)
            for bs, _bt in tgt_bound_tables
        )
        if (
            only_bound_tables
            and not probe_saved_tables
            and not src_has_bound_in_scope
            and not tgt_has_bound_in_scope
        ):
            if sel_src_schemas or sel_tgt_schemas:
                raise ValueError(
                    "所选 Schema 范围内暂无已绑定数据标准的映射表对或单库绑标表（可调整 Schema 或取消勾选「仅包含已绑标表」）"
                )
            raise ValueError(
                "当前跨环境来源库与目标库均暂无已绑定数据标准的映射表对或库表（请先绑定数据标准，或取消勾选「仅包含已绑标表」导出全量报告）"
            )

        src_cat = (
            self.ctx.metadata_hub.get_catalog_tree(src_cid, auto_refresh=False)
            if src_cid
            else {"tables": []}
        )
        tgt_cat = (
            src_cat
            if same_cid
            else (
                self.ctx.metadata_hub.get_catalog_tree(tgt_cid, auto_refresh=False)
                if tgt_cid
                else {"tables": []}
            )
        )

        need_src_refresh = bool(src_cid and not (src_cat.get("tables") or []))
        need_tgt_refresh = bool(tgt_cid and not same_cid and not (tgt_cat.get("tables") or []))

        warnings: List[str] = []
        catalogs_auto_refreshed = False

        if need_src_refresh or need_tgt_refresh:
            catalogs_auto_refreshed = True
            report(
                8.0,
                "采集全库目录",
                "检测到未缓存目录的数据源，先自动触发全库表目录采集...",
                0,
                2 if (need_src_refresh and need_tgt_refresh) else 1,
            )

            def _do_refresh(cid: str, label: str) -> Tuple[Optional[Dict[str, Any]], str]:
                try:
                    cat = self.ctx.metadata_hub.refresh_catalog(cid)
                    return cat, ""
                except Exception as exc:
                    return None, f"{label}自动采集目录失败: {exc}"

            if need_src_refresh and need_tgt_refresh:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    src_rf_fut = pool.submit(
                        _do_refresh, src_cid, f"来源库[{src_conn.get('name', src_cid)}]"
                    )
                    tgt_rf_fut = pool.submit(
                        _do_refresh, tgt_cid, f"目标库[{tgt_conn.get('name', tgt_cid)}]"
                    )
                    res_src, err_src = src_rf_fut.result()
                    if res_src is not None:
                        src_cat = res_src
                    if err_src:
                        warnings.append(err_src)
                    res_tgt, err_tgt = tgt_rf_fut.result()
                    if res_tgt is not None:
                        tgt_cat = res_tgt
                    if err_tgt:
                        warnings.append(err_tgt)
            elif need_src_refresh:
                res_src, err_src = _do_refresh(
                    src_cid, f"来源库[{src_conn.get('name', src_cid)}]"
                )
                if res_src is not None:
                    src_cat = res_src
                    if same_cid:
                        tgt_cat = res_src
                if err_src:
                    warnings.append(err_src)
            elif need_tgt_refresh:
                res_tgt, err_tgt = _do_refresh(
                    tgt_cid, f"目标库[{tgt_conn.get('name', tgt_cid)}]"
                )
                if res_tgt is not None:
                    tgt_cat = res_tgt
                if err_tgt:
                    warnings.append(err_tgt)

        raw_src_cat_tables = src_cat.get("tables") or []
        raw_tgt_cat_tables = tgt_cat.get("tables") or []
        src_cat_tables = [
            t
            for t in raw_src_cat_tables
            if _src_schema_matches(
                clean_identifier_input(str(t.get("schema_name") or src_def_sch)) or src_def_sch
            )
        ]
        tgt_cat_tables = [
            t
            for t in raw_tgt_cat_tables
            if _tgt_schema_matches(
                clean_identifier_input(str(t.get("schema_name") or tgt_def_sch)) or tgt_def_sch
            )
        ]

        if not probe_saved_tables and not src_cat_tables and not tgt_cat_tables:
            if warnings:
                raise ValueError("；".join(warnings))
            if sel_src_schemas or sel_tgt_schemas:
                raise ValueError("所选 Schema 范围内暂无可导出的映射表对或库表")
            raise ValueError("当前跨环境来源库与目标库均暂无可导出的映射表对或库表")

        src_metas_map: Optional[Dict[Tuple[str, str], TableMeta]] = None
        tgt_metas_map: Optional[Dict[Tuple[str, str], TableMeta]] = None
        src_skipped: Optional[Set[Tuple[str, str]]] = None
        tgt_skipped: Optional[Set[Tuple[str, str]]] = None
        src_probe_warn = ""
        tgt_probe_warn = ""

        if probe_saved_tables:
            src_targets: List[Tuple[str, str]] = []
            tgt_targets: List[Tuple[str, str]] = []
            seen_s_t: Set[Tuple[str, str]] = set()
            seen_t_t: Set[Tuple[str, str]] = set()
            for tb in probe_saved_tables:
                ss = clean_identifier_input(str(tb.get("source_schema") or src_def_sch)) or src_def_sch
                st = clean_identifier_input(str(tb.get("source_table") or tb.get("std_table_name") or ""))
                if st and (ss.lower(), st.lower()) not in seen_s_t:
                    seen_s_t.add((ss.lower(), st.lower()))
                    src_targets.append((ss, st))
                ts = clean_identifier_input(str(tb.get("target_schema") or tgt_def_sch)) or tgt_def_sch
                tt = clean_identifier_input(str(tb.get("target_table") or tb.get("std_table_name") or ""))
                if tt and (ts.lower(), tt.lower()) not in seen_t_t:
                    seen_t_t.add((ts.lower(), tt.lower()))
                    tgt_targets.append((ts, tt))

            report(
                20.0,
                "批量加载已保存表对结构",
                f"正在批量探查 {len(probe_saved_tables)} 组已保存表对结构（未配对表不自动预比对）...",
                0,
                len(probe_saved_tables),
            )
            if same_cid and (src_targets or tgt_targets):
                merged_targets: List[Tuple[str, str]] = []
                seen_merged: Set[Tuple[str, str]] = set()
                for pair_item in src_targets + tgt_targets:
                    mk = (pair_item[0].lower(), pair_item[1].lower())
                    if mk not in seen_merged:
                        seen_merged.add(mk)
                        merged_targets.append(pair_item)
                shared_map, shared_warn, shared_skip = (
                    self.ctx.metadata_hub.get_tables_metadata_batch(
                        src_cid,
                        merged_targets,
                        force_refresh=False,
                        penetrate_views=True,
                        tolerate_errors=True,
                    )
                )
                src_metas_map = shared_map
                tgt_metas_map = shared_map
                src_skipped = shared_skip
                tgt_skipped = shared_skip
                src_probe_warn = shared_warn
                tgt_probe_warn = shared_warn
                if shared_warn:
                    warnings.append(f"数据源探查受限: {shared_warn}")
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    src_meta_fut = (
                        pool.submit(
                            self.ctx.metadata_hub.get_tables_metadata_batch,
                            src_cid,
                            src_targets,
                            force_refresh=False,
                            penetrate_views=True,
                            tolerate_errors=True,
                        )
                        if src_cid and src_targets
                        else None
                    )
                    tgt_meta_fut = (
                        pool.submit(
                            self.ctx.metadata_hub.get_tables_metadata_batch,
                            tgt_cid,
                            tgt_targets,
                            force_refresh=False,
                            penetrate_views=True,
                            tolerate_errors=True,
                        )
                        if tgt_cid and tgt_targets
                        else None
                    )
                    if src_meta_fut is not None:
                        src_metas_map, src_probe_warn, src_skipped = src_meta_fut.result()
                        if src_probe_warn:
                            warnings.append(f"来源库探查受限: {src_probe_warn}")
                    if tgt_meta_fut is not None:
                        tgt_metas_map, tgt_probe_warn, tgt_skipped = tgt_meta_fut.result()
                        if tgt_probe_warn:
                            warnings.append(f"目标库探查受限: {tgt_probe_warn}")

        pairs: List[Dict[str, Any]] = []
        skipped_unbound_pairs = 0
        tot_pairs = len(probe_saved_tables)
        p_step = 1 if tot_pairs <= 25 else 5
        agg = {
            "saved_pairs_count": 0,
            "recommended_pairs_count": 0,
            "source_total_tables": len(src_cat_tables),
            "target_total_tables": len(tgt_cat_tables),
            "unified_cols": 0,
            "split_diff_cols": 0,
            "split_partial_cols": 0,
            "total_center_rows": 0,
            "target_needs_migration_cols": 0,
            "capacity_insufficient_cols": 0,
            "capacity_risk_cols": 0,
            "type_conversion_cols": 0,
            "unbound_src_cols": 0,
            "unbound_tgt_cols": 0,
        }

        # 批量预加载全部涉及表对的已保存列配对、两侧第 2 步绑标与标准字典，消除报告逐表 SQLite 连接/查询风暴
        saved_cols_by_key: Dict[str, List[Dict[str, Any]]] = {}
        src_binds_by_tbl: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        tgt_binds_by_tbl: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        prefetched_std_dict_map: Dict[str, Dict[str, Any]] = {}

        if probe_saved_tables:
            wanted_keys = [
                str(tb.get("std_table_key") or "").strip().lower()
                for tb in probe_saved_tables
                if str(tb.get("std_table_key") or "").strip()
            ]
            if wanted_keys:
                with self.ctx.store.module_db() as conn:
                    for i in range(0, len(wanted_keys), 400):
                        chunk = wanted_keys[i : i + 400]
                        ph = ",".join("?" for _ in chunk)
                        c_rows = conn.execute(
                            f"""
                            SELECT * FROM cross_column_bindings
                            WHERE LOWER(std_table_key) IN ({ph})
                            ORDER BY row_index ASC, id ASC
                            """,
                            chunk,
                        ).fetchall()
                        for r in c_rows:
                            rd = dict(r)
                            tk = str(rd.get("std_table_key") or "").strip().lower()
                            saved_cols_by_key.setdefault(tk, []).append(rd)

            src_bind_targets: List[Tuple[str, str]] = []
            tgt_bind_targets: List[Tuple[str, str]] = []
            for tb in probe_saved_tables:
                ss = clean_identifier_input(str(tb.get("source_schema") or src_def_sch)) or src_def_sch
                st = clean_identifier_input(str(tb.get("source_table") or tb.get("std_table_name") or ""))
                if st:
                    src_bind_targets.append((ss, st))
                    if src_metas_map:
                        sm = src_metas_map.get((ss.lower(), st.lower()))
                        if sm and sm.table_name:
                            src_bind_targets.append((sm.schema_name or ss, sm.table_name))
                ts = clean_identifier_input(str(tb.get("target_schema") or tgt_def_sch)) or tgt_def_sch
                tt = clean_identifier_input(str(tb.get("target_table") or tb.get("std_table_name") or ""))
                if tt:
                    tgt_bind_targets.append((ts, tt))
                    if tgt_metas_map:
                        tm = tgt_metas_map.get((ts.lower(), tt.lower()))
                        if tm and tm.table_name:
                            tgt_bind_targets.append((tm.schema_name or ts, tm.table_name))

            if src_cid and src_bind_targets:
                src_binds_by_tbl = self.ctx.standard_catalog.get_tables_field_bindings_batch(
                    src_cid, src_bind_targets
                )
            if tgt_cid and tgt_bind_targets:
                if same_cid and tgt_bind_targets == src_bind_targets:
                    tgt_binds_by_tbl = src_binds_by_tbl
                else:
                    tgt_binds_by_tbl = self.ctx.standard_catalog.get_tables_field_bindings_batch(
                        tgt_cid, tgt_bind_targets
                    )

            all_pref_codes: List[str] = []
            for cols_list in saved_cols_by_key.values():
                for sc in cols_list:
                    c1 = clean_excel_cell_str(sc.get("source_std_code"), treat_placeholder_empty=True)
                    c2 = clean_excel_cell_str(sc.get("target_std_code"), treat_placeholder_empty=True)
                    if c1:
                        all_pref_codes.append(c1)
                    if c2:
                        all_pref_codes.append(c2)
            for bmap in src_binds_by_tbl.values():
                for br in bmap.values():
                    sc_val = (br.get("std_code") or "").strip()
                    if sc_val:
                        all_pref_codes.append(sc_val)
            for bmap in tgt_binds_by_tbl.values():
                for br in bmap.values():
                    tc_val = (br.get("std_code") or "").strip()
                    if tc_val:
                        all_pref_codes.append(tc_val)
            if all_pref_codes:
                prefetched_std_dict_map = self.ctx.standard_catalog.get_items_by_codes(
                    all_pref_codes
                )

        def _pick_prefetched_binds(
            binds_map: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]],
            metas_map: Optional[Dict[Tuple[str, str], TableMeta]],
            cfg_schema: str,
            cfg_table: str,
        ) -> Dict[str, Dict[str, Any]]:
            cs = clean_identifier_input(cfg_schema or "").lower() or "public"
            ct = clean_identifier_input(cfg_table or "").lower()
            if not ct:
                return {}
            m_obj = metas_map.get((cs, ct)) if metas_map else None
            if m_obj and m_obj.exists:
                ak = (
                    (clean_identifier_input(m_obj.schema_name or "") or cs).lower(),
                    (clean_identifier_input(m_obj.table_name or "") or ct).lower(),
                )
                hit = binds_map.get(ak)
                if hit is not None:
                    return hit
            return binds_map.get((cs, ct)) or {}

        for idx, t in enumerate(probe_saved_tables, start=1):
            if idx == 1 or idx % p_step == 0 or idx == tot_pairs:
                pct = 48.0 + 38.0 * (idx / max(1, tot_pairs))
                report(
                    pct,
                    "逐对计算五虚拟列比对",
                    f"[{idx}/{tot_pairs}] 对标比对 {t.get('std_table_key')}...",
                    idx,
                    tot_pairs,
                )
            t_key_low = str(t.get("std_table_key") or "").strip().lower()
            s_sch_pair = clean_identifier_input(str(t.get("source_schema") or src_def_sch)) or src_def_sch
            s_tbl_pair = clean_identifier_input(str(t.get("source_table") or t.get("std_table_name") or ""))
            t_sch_pair = clean_identifier_input(str(t.get("target_schema") or tgt_def_sch)) or tgt_def_sch
            t_tbl_pair = clean_identifier_input(str(t.get("target_table") or t.get("std_table_name") or ""))
            comp = self.compare_single_table(
                t["std_table_key"],
                force_refresh=False,
                format_profile=format_profile,
                source_format_profile=eff_src_fmt,
                target_format_profile=eff_tgt_fmt,
                persist_missing_binding=False,
                load_saved_binding=True,
                src_conn=src_conn,
                tgt_conn=tgt_conn,
                prefetched_src_metas=src_metas_map,
                prefetched_tgt_metas=tgt_metas_map,
                prefetched_src_skipped=src_skipped,
                prefetched_tgt_skipped=tgt_skipped,
                prefetched_src_warning=src_probe_warn,
                prefetched_tgt_warning=tgt_probe_warn,
                prefetched_table_binding=t,
                prefetched_saved_cols=saved_cols_by_key.get(t_key_low, []),
                prefetched_src_field_binds=_pick_prefetched_binds(
                    src_binds_by_tbl, src_metas_map, s_sch_pair, s_tbl_pair
                ),
                prefetched_tgt_field_binds=_pick_prefetched_binds(
                    tgt_binds_by_tbl, tgt_metas_map, t_sch_pair, t_tbl_pair
                ),
                prefetched_std_dict_map=prefetched_std_dict_map,
            )
            hydrated = self._hydrate_five_col_pair(comp)
            if only_bound_tables and len(hydrated["center_rows"]) == 0:
                skipped_unbound_pairs += 1
                continue
            pairs.append(hydrated)

            bk_s = hydrated["bookmark_summary"]
            agg["unified_cols"] += int(bk_s.get("unified") or 0)
            agg["split_diff_cols"] += int(bk_s.get("split_diff") or 0)
            agg["split_partial_cols"] += int(bk_s.get("split_partial") or 0)
            agg["total_center_rows"] += len(hydrated["center_rows"])
            agg["target_needs_migration_cols"] += int(bk_s.get("target_needs_migration") or 0)
            agg["capacity_insufficient_cols"] += int(bk_s.get("capacity_insufficient") or 0)
            agg["capacity_risk_cols"] += int(bk_s.get("capacity_risk") or 0)
            agg["type_conversion_cols"] += int(bk_s.get("type_conversion") or 0)
            agg["unbound_src_cols"] += len(hydrated["source_candidates"])
            agg["unbound_tgt_cols"] += len(hydrated["target_candidates"])

        report(90.0, "生成推荐同名表清单", "正在扫描未配对同名表（仅推荐不预比对）...", 0, 0)
        src_bind_stats = (
            self.ctx.standard_catalog.get_connection_binding_stats(src_cid) if src_cid else {}
        )
        tgt_bind_stats = (
            src_bind_stats
            if same_cid
            else (self.ctx.standard_catalog.get_connection_binding_stats(tgt_cid) if tgt_cid else {})
        )
        raw_recommendations, raw_source_only, raw_target_only = self._compute_recommended_pairings(
            saved_tables=all_saved_tables,
            src_cat_tables=src_cat_tables,
            tgt_cat_tables=tgt_cat_tables,
            src_def_schema=src_def_sch,
            tgt_def_schema=tgt_def_sch,
            src_bind_stats=src_bind_stats,
            tgt_bind_stats=tgt_bind_stats,
            src_db_type=src_db_type,
            tgt_db_type=tgt_db_type,
        )
        if only_bound_tables:
            recommendations = [
                r
                for r in raw_recommendations
                if int(r.get("source_bound_count") or 0) > 0
                or int(r.get("target_bound_count") or 0) > 0
            ]
            source_only = [t for t in raw_source_only if int(t.get("bound_count") or 0) > 0]
            target_only = [t for t in raw_target_only if int(t.get("bound_count") or 0) > 0]
            skipped_unbound_tables_count = (
                (len(schema_saved_tables) - len(probe_saved_tables))
                + skipped_unbound_pairs
                + (len(raw_recommendations) - len(recommendations))
                + (len(raw_source_only) - len(source_only))
                + (len(raw_target_only) - len(target_only))
            )
        else:
            recommendations = raw_recommendations
            source_only = raw_source_only
            target_only = raw_target_only
            skipped_unbound_tables_count = 0

        if not pairs and not recommendations and not source_only and not target_only:
            if only_bound_tables:
                raise ValueError(
                    "所选范围内暂无已绑定数据标准的表对或库表（请先绑定数据标准，或取消勾选「仅包含已绑标表」导出全量报告）"
                )
            raise ValueError("所选范围内暂无可导出的映射表对或库表")

        agg["saved_pairs_count"] = len(pairs)
        agg["recommended_pairs_count"] = len(recommendations)

        scope_parts: List[str] = []
        if sel_src_schemas:
            s_txt = ", ".join(sel_src_schemas[:2]) + (
                f" 等{len(sel_src_schemas)}" if len(sel_src_schemas) > 2 else ""
            )
            scope_parts.append(f"源:{s_txt}")
        if sel_tgt_schemas:
            t_txt = ", ".join(sel_tgt_schemas[:2]) + (
                f" 等{len(sel_tgt_schemas)}" if len(sel_tgt_schemas) > 2 else ""
            )
            scope_parts.append(f"目标:{t_txt}")
        schema_part = " / ".join(scope_parts) if scope_parts else "全部 Schema"
        scope_label = (
            f"{schema_part} · 仅已绑标表"
            if only_bound_tables
            else ("全库全量" if not scope_parts else f"{schema_part} · 全部表")
        )

        report(
            98.0,
            "装配报告文件",
            f"已汇集 {len(pairs)} 组已配对表与 {len(recommendations)} 组推荐同名表，正在生成报告文件...",
            len(pairs),
            max(1, len(pairs)),
        )

        return {
            "report_type": "cross_env",
            "catalogs_auto_refreshed": catalogs_auto_refreshed,
            "report_scope": {
                "source_schemas": sel_src_schemas,
                "target_schemas": sel_tgt_schemas,
                "only_bound_tables": bool(only_bound_tables),
                "skipped_unbound_tables_count": skipped_unbound_tables_count,
                "scope_label": scope_label,
            },
            "source_connection": {
                "configured": bool(src_conn.get("configured")),
                "connection_id": src_cid,
                "name": src_conn.get("name", ""),
                "db_type": src_conn.get("db_type", ""),
                "default_schema": src_def_sch,
            },
            "target_connection": {
                "configured": bool(tgt_conn.get("configured")),
                "connection_id": tgt_cid,
                "name": tgt_conn.get("name", ""),
                "db_type": tgt_conn.get("db_type", ""),
                "default_schema": tgt_def_sch,
            },
            "source_format_profile": eff_src_fmt,
            "source_format_profile_label": fmt_map.get(eff_src_fmt, eff_src_fmt),
            "target_format_profile": eff_tgt_fmt,
            "target_format_profile_label": fmt_map.get(eff_tgt_fmt, eff_tgt_fmt),
            "aggregate": agg,
            "pairs": pairs,
            "recommended_pairings": recommendations,
            "source_only_tables": source_only,
            "target_only_tables": target_only,
            "warnings": warnings,
        }

    def export_cross_report_excel_bytes(
        self,
        *,
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
        source_schemas: Union[str, Sequence[str]] = "",
        target_schemas: Union[str, Sequence[str]] = "",
        only_bound_tables: bool = True,
        progress_cb: ProgressCallback = noop_progress,
    ) -> bytes:
        report_data = self.build_database_cross_report_data(
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
            source_schemas=source_schemas,
            target_schemas=target_schemas,
            only_bound_tables=only_bound_tables,
            progress_cb=progress_cb,
        )
        return build_cross_env_excel_bytes(report_data)

    def export_cross_report_html_bytes(
        self,
        *,
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
        source_schemas: Union[str, Sequence[str]] = "",
        target_schemas: Union[str, Sequence[str]] = "",
        only_bound_tables: bool = True,
        progress_cb: ProgressCallback = noop_progress,
    ) -> bytes:
        report_data = self.build_database_cross_report_data(
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
            source_schemas=source_schemas,
            target_schemas=target_schemas,
            only_bound_tables=only_bound_tables,
            progress_cb=progress_cb,
        )
        return build_cross_env_html_bytes(report_data)
