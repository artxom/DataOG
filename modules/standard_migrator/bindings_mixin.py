"""单库落标贯标模块 — 字段绑标、三级落标核验、批量对照导入与转贯标队列混入类."""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Set, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.platform.dialects import clean_identifier_input
from core.platform.excel_io import (
    ResolvedWorkbook,
    clean_excel_cell_str,
    validate_xlsx_bytes,
)
from core.platform.import_progress import ProgressCallback, noop_progress


class _StandardBindingsMixin:
    """第 2 步核心闭环：字段绑标、智能预匹配、落标检查、对照模板/上传与纳入贯标队列."""

    # =========================================================================
    # 第 2 步核心闭环：字段绑标 + 三维分级落标核验 + 对照模板/上传 + 转贯标任务
    # =========================================================================

    @staticmethod
    def _parse_table_targets(
        tables: Optional[List[Any]] = None,
        schema_name: str = "",
        table_name: str = "",
        default_schema: str = "public",
    ) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        seen = set()

        def _add(sch: str, tbl: str) -> None:
            t_clean = clean_identifier_input(tbl)
            if not t_clean:
                return
            s_clean = clean_identifier_input(sch or default_schema) or default_schema or "public"
            key = (s_clean.lower(), t_clean.lower())
            if key not in seen:
                seen.add(key)
                out.append((s_clean, t_clean))

        if tables:
            for item in tables:
                if isinstance(item, dict):
                    _add(str(item.get("schema_name") or ""), str(item.get("table_name") or ""))
                elif isinstance(item, str):
                    txt = item.strip()
                    if "." in txt:
                        p0, p1 = txt.split(".", 1)
                        _add(p0, p1)
                    else:
                        _add(default_schema, txt)

        if table_name:
            if "." in table_name:
                p0, p1 = table_name.split(".", 1)
                _add(schema_name or p0, p1)
            else:
                _add(schema_name or default_schema, table_name)

        return out

    def _resolve_binding_profile(self, connection_id: Optional[str] = None) -> Optional[Any]:
        cid = (connection_id or "").strip() or self.get_active_connection_id()
        if not cid:
            return None
        return self.ctx.connection_hub.get_profile(cid)

    @staticmethod
    def _resolve_format_profile(prof: Optional[Any], requested: str = "AUTO") -> str:
        req = (requested or "").strip()
        if req and req != "AUTO":
            return req
        if prof is not None:
            saved = (getattr(prof, "standard_format", "") or "").strip()
            if saved:
                return saved
        return "AUTO"

    def get_bindings_overview(
        self,
        connection_id: str = "",
        format_profile: str = "AUTO",
        *,
        include_catalog: bool = False,
    ) -> Dict[str, Any]:
        prof = self._resolve_binding_profile(connection_id)
        cid = prof.connection_id if prof else ""
        eff_format = self._resolve_format_profile(prof, format_profile)
        if prof and prof.connection_id != self.ctx.store.get_meta("migrator_connection_id", ""):
            # 允许查看不同连接时不强制破坏历史执行台绑定，若当前未绑定则顺便绑定
            if not self.ctx.store.get_meta("migrator_connection_id", ""):
                self.ctx.store.save_meta("migrator_connection_id", prof.connection_id)

        if cid:
            self.ctx.standard_catalog.purge_invalid_error_bindings(cid)
        catalog_tree = (
            self.ctx.metadata_hub.get_catalog_tree(cid, auto_refresh=False)
            if (include_catalog and cid)
            else {"schemas": [], "tables": []}
        )
        return {
            "ok": True,
            "active_connection_id": cid,
            "active_connection": prof.to_public_dict() if prof else None,
            "connections": self.ctx.connection_hub.list_profiles(),
            "catalog": catalog_tree,
            "dict_summary": self.ctx.standard_catalog.get_summary(),
            "format_profiles": self.ctx.standard_catalog.list_format_profiles(),
            "format_profile": eff_format,
            "table_stats": self.ctx.standard_catalog.get_connection_binding_stats(cid) if cid else {},
            "migration_summary": self.ctx.store.get_summary(),
        }

    def _evaluate_table_meta_compliance(
        self,
        *,
        prof: Any,
        eff_schema: str,
        eff_table: str,
        t_meta: Any,
        bindings_map: Dict[str, Dict[str, Any]],
        std_dict_map: Dict[str, Dict[str, Any]],
        format_profile: str = "AUTO",
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        actual_schema = t_meta.schema_name or eff_schema
        actual_table = t_meta.table_name or eff_table

        dist_set = {k.lower() for k in (t_meta.distribute_keys or [])}
        part_set = {k.lower() for k in (t_meta.partition_keys or [])}
        pk_set = {k.lower() for k in (t_meta.primary_keys or [])}
        is_col_store = str(t_meta.orientation or "").upper() == "COLUMN"

        columns_out: List[Dict[str, Any]] = []
        compliance_updates: List[Dict[str, Any]] = []
        counts = {
            "total_cols": len(t_meta.columns),
            "bound_cols": 0,
            "compliant_cols": 0,
            "comment_only_cols": 0,
            "non_compliant_cols": 0,
            "std_missing_cols": 0,
            "unbound_cols": 0,
        }

        for col in t_meta.columns:
            col_clean = clean_identifier_input(col.column_name) or col.column_name
            col_lower = col_clean.lower()
            bind_rec = bindings_map.get(col_lower)
            std_code = (bind_rec.get("std_code") if bind_rec else "") or ""
            std_item = std_dict_map.get(std_code) if std_code else None

            ev = self.ctx.standard_catalog.evaluate_column_compliance(
                col_name=col_clean,
                col_type=col.data_type,
                col_comment=col.column_comment,
                std_item=std_item,
                std_code=std_code,
                db_type=prof.db_type,
                format_profile=format_profile,
            )

            st = ev["status"]
            if std_code:
                counts["bound_cols"] += 1
                upd_schema = (bind_rec.get("schema_name") if bind_rec else "") or actual_schema
                upd_table = (bind_rec.get("table_name") if bind_rec else "") or actual_table
                compliance_updates.append(
                    {
                        "schema_name": upd_schema,
                        "table_name": upd_table,
                        "column_name": col_clean,
                        "compliance_status": st,
                        "compliance_detail": ev["detail"],
                    }
                )
            if st == "COMPLIANT":
                counts["compliant_cols"] += 1
            elif st == "COMMENT_ONLY":
                counts["comment_only_cols"] += 1
            elif st == "NON_COMPLIANT":
                counts["non_compliant_cols"] += 1
            elif st == "STD_MISSING":
                counts["std_missing_cols"] += 1
            else:
                counts["unbound_cols"] += 1

            is_dist = col_lower in dist_set
            is_part = col_lower in part_set
            is_pk = bool(col.is_primary_key) or (col_lower in pk_set)
            needs_rebuild = bool(
                st == "NON_COMPLIANT"
                and prof.db_type in ("DWS", "PGSQL", "KINGBASE")
                and ((is_dist or is_part) or (is_col_store and not ev["type_ok"]))
            )

            columns_out.append(
                {
                    "ordinal_position": col.ordinal_position,
                    "column_name": col_clean,
                    "raw_column_name": col.column_name,
                    "data_type": col.data_type,
                    "normalized_type": col.normalized_type,
                    "column_comment": col.column_comment,
                    "is_not_null": col.is_not_null,
                    "is_primary_key": is_pk,
                    "is_distribute_key": is_dist,
                    "is_partition_key": is_part,
                    "needs_rebuild": needs_rebuild,
                    "std_code": std_code,
                    "bind_source": (bind_rec.get("bind_source") if bind_rec else "") or "",
                    "std_cn_name": std_item.get("cn_name", "") if std_item else "",
                    "std_en_abbr": std_item.get("en_abbr", "") if std_item else "",
                    "std_target_type": ev["target_type"],
                    "std_domain_name": std_item.get("domain_name", "") if std_item else "",
                    "std_biz_definition": std_item.get("biz_definition", "") if std_item else "",
                    "compliance_status": st,
                    "compliance_label": ev["label"],
                    "compliance_detail": ev["detail"],
                    "name_ok": ev["name_ok"],
                    "type_ok": ev["type_ok"],
                    "comment_ok": ev["comment_ok"],
                }
            )

        total_c = counts["total_cols"]
        counts["coverage_pct"] = (
            round(counts["bound_cols"] * 100.0 / total_c, 1) if total_c > 0 else 0.0
        )
        counts["compliant_pct"] = (
            round(counts["compliant_cols"] * 100.0 / total_c, 1) if total_c > 0 else 0.0
        )

        res_dict: Dict[str, Any] = {
            "ok": True,
            "connection_id": prof.connection_id,
            "connection_name": prof.name,
            "db_type": prof.db_type,
            "format_profile": format_profile or "AUTO",
            "table": {
                "schema_name": actual_schema,
                "table_name": actual_table,
                "exists": t_meta.exists,
                "object_type": t_meta.object_type,
                "table_comment": t_meta.table_comment,
                "orientation": t_meta.orientation,
                "distribute_keys": list(t_meta.distribute_keys or []),
                "partition_keys": list(t_meta.partition_keys or []),
            },
            "summary": counts,
            "columns": columns_out,
        }
        return res_dict, compliance_updates

    def _get_table_compliance_with_meta(
        self,
        connection_id: str,
        schema_name: str,
        table_name: str,
        *,
        format_profile: str = "AUTO",
        force_refresh: bool = False,
        include_table_stats: bool = True,
        purge_errors: bool = True,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Tuple[Dict[str, Any], Any]:
        report = progress_cb or noop_progress
        prof = self.ctx.connection_hub.require_profile(connection_id)
        eff_format = self._resolve_format_profile(prof, format_profile)
        def_sch = prof.effective_default_schema()
        eff_schema = (schema_name or def_sch or "public").strip()
        eff_table = (table_name or "").strip()
        if "." in eff_table and not schema_name:
            eff_schema, eff_table = eff_table.split(".", 1)
        eff_schema = clean_identifier_input(eff_schema) or def_sch or "public"
        eff_table = clean_identifier_input(eff_table)
        if not eff_table:
            raise ValueError("表名不能为空")

        meta_cb: ProgressCallback = noop_progress
        if report is not noop_progress:
            def _scaled_meta_cb(
                pct: float, stage: str, detail: str = "", processed: int = 0, total: int = 0
            ) -> None:
                scaled = 15.0 + (min(100.0, max(0.0, float(pct))) / 100.0) * 53.0
                report(scaled, stage, detail, processed, total)

            meta_cb = _scaled_meta_cb

        t_meta = self.ctx.metadata_hub.get_table_metadata(
            prof.connection_id,
            schema_name=eff_schema,
            table_name=eff_table,
            force_refresh=force_refresh,
            penetrate_views=True,
            progress_cb=meta_cb,
        )
        actual_schema = t_meta.schema_name or eff_schema
        actual_table = t_meta.table_name or eff_table
        col_total = len(t_meta.columns)

        if t_meta.exists and t_meta.columns:
            live_col_names = [c.column_name for c in t_meta.columns if c.column_name]
            self.ctx.standard_catalog.prune_stale_table_bindings(
                prof.connection_id, actual_schema, actual_table, live_col_names
            )
            if eff_schema.lower() != actual_schema.lower():
                self.ctx.standard_catalog.prune_stale_table_bindings(
                    prof.connection_id, eff_schema, actual_table, live_col_names
                )

        report(
            74.0,
            "匹配数据标准",
            f"正在加载 {actual_schema}.{actual_table} 绑标记录与标准字典...",
            0,
            col_total,
        )
        if purge_errors:
            self.ctx.standard_catalog.purge_invalid_error_bindings(prof.connection_id)
        bindings_map: Dict[str, Dict[str, Any]] = {}
        if eff_schema.lower() != actual_schema.lower():
            bindings_map.update(
                self.ctx.standard_catalog.get_table_field_bindings(
                    prof.connection_id, eff_schema, actual_table
                )
            )
        actual_binds = self.ctx.standard_catalog.get_table_field_bindings(
            prof.connection_id, actual_schema, actual_table
        )
        if not actual_binds and not bindings_map and actual_schema.lower() != "public":
            bindings_map.update(
                self.ctx.standard_catalog.get_table_field_bindings(
                    prof.connection_id, "public", actual_table
                )
            )
        bindings_map.update(actual_binds)
        std_codes = [
            b.get("std_code", "")
            for b in bindings_map.values()
            if (b.get("std_code") or "").strip()
        ]
        std_dict_map = self.ctx.standard_catalog.get_items_by_codes(std_codes)

        report(
            86.0,
            "核验字段落标状态",
            f"正在对 {actual_schema}.{actual_table} 共 {col_total} 列执行三维落标核验...",
            col_total,
            col_total,
        )
        res_dict, compliance_updates = self._evaluate_table_meta_compliance(
            prof=prof,
            eff_schema=eff_schema,
            eff_table=eff_table,
            t_meta=t_meta,
            bindings_map=bindings_map,
            std_dict_map=std_dict_map,
            format_profile=eff_format,
        )
        if compliance_updates:
            self.ctx.standard_catalog.update_binding_compliance_batch(
                prof.connection_id, compliance_updates
            )
        if include_table_stats:
            res_dict["table_stats"] = self.ctx.standard_catalog.get_connection_binding_stats(
                prof.connection_id
            )
        report(
            96.0,
            "核验字段落标状态",
            f"已完成 {actual_schema}.{actual_table} 落标判定（已落标 {res_dict['summary']['compliant_cols']}/{col_total}）",
            col_total,
            col_total,
        )
        return res_dict, t_meta

    def get_table_compliance(
        self,
        connection_id: str,
        schema_name: str,
        table_name: str,
        *,
        format_profile: str = "AUTO",
        force_refresh: bool = False,
        include_table_stats: bool = True,
        purge_errors: bool = True,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        res_dict, _ = self._get_table_compliance_with_meta(
            connection_id,
            schema_name,
            table_name,
            format_profile=format_profile,
            force_refresh=force_refresh,
            include_table_stats=include_table_stats,
            purge_errors=purge_errors,
            progress_cb=progress_cb,
        )
        return res_dict

    def save_field_bindings(
        self,
        *,
        connection_id: str,
        bindings: List[Dict[str, Any]],
        format_profile: str = "AUTO",
        active_schema: str = "",
        active_table: str = "",
    ) -> Dict[str, Any]:
        prof = self.ctx.connection_hub.require_profile(connection_id)
        eff_format = self._resolve_format_profile(prof, format_profile)
        self.ctx.standard_catalog.upsert_field_bindings(
            prof.connection_id, bindings, bind_source="MANUAL"
        )
        sch = active_schema or (bindings[0].get("schema_name", "") if bindings else "")
        tbl = active_table or (bindings[0].get("table_name", "") if bindings else "")
        if tbl:
            res = self.get_table_compliance(
                prof.connection_id,
                sch or prof.effective_default_schema(),
                tbl,
                format_profile=eff_format,
                force_refresh=False,
            )
            res["message"] = f"已保存 {actual_len_text(bindings)} 条字段标准绑定"
            return res
        return {
            "ok": True,
            "format_profile": eff_format,
            "table_stats": self.ctx.standard_catalog.get_connection_binding_stats(prof.connection_id),
            "message": "字段标准绑定已更新",
        }

    def auto_match_bindings(
        self,
        *,
        connection_id: str,
        tables: Optional[List[Any]] = None,
        schema_name: str = "",
        table_name: str = "",
        format_profile: str = "AUTO",
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        prof = self.ctx.connection_hub.require_profile(connection_id)
        eff_format = self._resolve_format_profile(prof, format_profile)
        targets = self._parse_table_targets(
            tables=tables,
            schema_name=schema_name,
            table_name=table_name,
            default_schema=prof.effective_default_schema(),
        )
        if not targets:
            tree = self.ctx.metadata_hub.get_catalog_tree(prof.connection_id, auto_refresh=False)
            targets = [
                (t["schema_name"], t["table_name"])
                for t in (tree.get("tables") or [])[:50]
            ]
        if not targets:
            raise ValueError("当前连接暂无可匹配的数据表，请先刷新元数据或选择目标表")

        tot_targets = len(targets)
        report(
            18.0,
            "加载目标表结构",
            f"正在读取 {tot_targets} 张表的字段与注释元数据...",
            0,
            tot_targets,
        )

        def _on_auto_probe(done_cnt: int, tot_cnt: int, cur_s: str, cur_t: str) -> None:
            pct = 18.0 + 28.0 * (min(done_cnt, max(1, tot_cnt)) / max(1, tot_cnt))
            report(
                pct,
                "加载目标表结构",
                f"[{min(done_cnt + 1, tot_cnt)}/{tot_cnt}] 获取库表结构 {cur_s}.{cur_t}...",
                done_cnt,
                tot_cnt,
            )

        matched_count = 0
        to_upsert: List[Dict[str, Any]] = []
        metas_map, _, _ = self.ctx.metadata_hub.get_tables_metadata_batch(
            prof.connection_id,
            targets,
            force_refresh=False,
            on_progress=_on_auto_probe,
        )
        lookup_pairs = list(targets)
        for m_obj in metas_map.values():
            if m_obj and m_obj.table_name:
                lookup_pairs.append((m_obj.schema_name, m_obj.table_name))
        existing_batch = self.ctx.standard_catalog.get_tables_field_bindings_batch(
            prof.connection_id, lookup_pairs
        )
        pending_queries: List[Tuple[str, str]] = []
        for sch, tbl in targets:
            t_meta = metas_map.get((sch.lower(), tbl.lower()))
            if t_meta is None:
                continue
            actual_s = t_meta.schema_name or sch
            actual_t = t_meta.table_name or tbl
            existing = existing_batch.get((actual_s.lower(), actual_t.lower())) or existing_batch.get(
                (sch.lower(), actual_t.lower()), {}
            )
            for col in t_meta.columns:
                col_clean = clean_identifier_input(col.column_name) or col.column_name
                cur_bind = existing.get(col_clean.lower())
                if cur_bind and (cur_bind.get("std_code") or "").strip():
                    continue
                pending_queries.append((col_clean, (col.column_comment or "").strip()))

        report(
            48.0,
            "模糊匹配标准字典",
            f"正在检索 {len(pending_queries)} 个未绑字段的英文简称与中文标准候选...",
            0,
            max(1, len(pending_queries)),
        )
        cand_cache = self.ctx.standard_catalog.match_standard_candidates_batch(pending_queries)

        for idx_t, (sch, tbl) in enumerate(targets, start=1):
            t_meta = metas_map.get((sch.lower(), tbl.lower()))
            if t_meta is not None:
                actual_s = t_meta.schema_name or sch
                actual_t = t_meta.table_name or tbl
                existing = existing_batch.get(
                    (actual_s.lower(), actual_t.lower())
                ) or existing_batch.get((sch.lower(), actual_t.lower()), {})
                for col in t_meta.columns:
                    col_clean = clean_identifier_input(col.column_name) or col.column_name
                    cur_bind = existing.get(col_clean.lower())
                    if cur_bind and (cur_bind.get("std_code") or "").strip():
                        continue
                    ck = (col_clean.lower(), (col.column_comment or "").strip())
                    cands = cand_cache.get(ck) or []
                    if not cands:
                        continue
                    # 当仅 1 个精确命中，或首位为标准字典且与注释/英文简称强一致时采用
                    picked = cands[0]
                    exact_en = (
                        clean_identifier_input(picked.get("en_abbr") or "").lower()
                        == col_clean.lower()
                    )
                    exact_cn = (
                        bool(col.column_comment)
                        and picked.get("cn_name") == col.column_comment.strip()
                    )
                    if (
                        len(cands) == 1
                        or (exact_en and exact_cn)
                        or picked.get("is_standard_dict") == "1"
                    ):
                        to_upsert.append(
                            {
                                "schema_name": actual_s,
                                "table_name": actual_t,
                                "column_name": col_clean,
                                "std_code": picked["std_code"],
                            }
                        )
                        matched_count += 1
            if idx_t == 1 or idx_t % 5 == 0 or idx_t == tot_targets:
                report(
                    52.0 + 12.0 * (idx_t / max(1, tot_targets)),
                    "模糊匹配标准字典",
                    f"[{idx_t}/{tot_targets}] 匹配 {sch}.{tbl}（累计命中 {matched_count} 项）...",
                    idx_t,
                    tot_targets,
                )

        if to_upsert:
            report(
                66.0,
                "保存匹配绑定",
                f"正在保存 {len(to_upsert)} 条智能匹配的标准绑定...",
                len(to_upsert),
                len(to_upsert),
            )
            self.ctx.standard_catalog.upsert_field_bindings(
                prof.connection_id, to_upsert, bind_source="AUTO_MATCH"
            )

        batch_check = self.check_compliance(
            connection_id=prof.connection_id,
            tables=[{"schema_name": s, "table_name": t} for s, t in targets],
            format_profile=eff_format,
            force_refresh=False,
            progress_cb=report,
            tolerate_probe_errors=True,
            progress_start_pct=70.0,
        )
        last_table_res = batch_check["tables"][-1] if batch_check.get("tables") else None

        res: Dict[str, Any] = {
            "ok": True,
            "format_profile": eff_format,
            "matched_count": matched_count,
            "checked_tables": len(targets),
            "table_stats": batch_check["table_stats"],
            "message": f"智能预匹配完成：共为 {matched_count} 个未绑字段自动关联数据标准",
        }
        if last_table_res and len(targets) == 1:
            res.update(
                {
                    "table": last_table_res["table"],
                    "summary": last_table_res["summary"],
                    "columns": last_table_res["columns"],
                }
            )
        return res

    def check_compliance(
        self,
        *,
        connection_id: str,
        tables: Optional[List[Any]] = None,
        format_profile: str = "AUTO",
        force_refresh: bool = True,
        progress_cb: Optional[ProgressCallback] = noop_progress,
        tolerate_probe_errors: bool = False,
        purge_errors: bool = True,
        progress_start_pct: float = 20.0,
        unqualified_tables: Optional[Set[Tuple[str, str]]] = None,
        realign_replace_mode: bool = False,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        prof = self.ctx.connection_hub.require_profile(connection_id)
        eff_format = self._resolve_format_profile(prof, format_profile)
        eff_def_schema = prof.effective_default_schema()
        targets = self._parse_table_targets(
            tables=tables,
            default_schema=eff_def_schema,
        )
        if not targets:
            targets = self.ctx.standard_catalog.list_bound_tables_for_connection(prof.connection_id)
        if not targets:
            tree = self.ctx.metadata_hub.get_catalog_tree(prof.connection_id, auto_refresh=False)
            targets = [
                (t["schema_name"], t["table_name"])
                for t in (tree.get("tables") or [])[:30]
            ]
        if not targets:
            raise ValueError("请先在左侧选择至少一张表或先上传字段对照关系")

        if purge_errors:
            self.ctx.standard_catalog.purge_invalid_error_bindings(prof.connection_id)

        total_targets = len(targets)
        start_pct = max(12.0, min(85.0, float(progress_start_pct)))
        probe_end_pct = start_pct + (94.0 - start_pct) * 0.48

        report(
            start_pct,
            "核验库表落标状态",
            f"准备核验 {total_targets} 张库表的字段绑定与落标状态...",
            0,
            total_targets,
        )

        def _on_meta_progress(done_cnt: int, tot_cnt: int, cur_s: str, cur_t: str) -> None:
            pct = start_pct + (probe_end_pct - start_pct) * (
                min(done_cnt, max(1, tot_cnt)) / max(1, tot_cnt)
            )
            report(
                pct,
                "核验库表落标状态",
                f"[{min(done_cnt + 1, tot_cnt)}/{tot_cnt}] 获取库表结构 {cur_s}.{cur_t}...",
                done_cnt,
                tot_cnt,
            )

        metas_map, probe_warning, skipped_keys = self.ctx.metadata_hub.get_tables_metadata_batch(
            prof.connection_id,
            targets,
            force_refresh=force_refresh,
            penetrate_views=True,
            tolerate_errors=tolerate_probe_errors,
            on_progress=_on_meta_progress,
        )

        # 单次批量预加载全部涉及表的字段绑定与引用标准字典，彻底消除逐表 SQLite 连接风暴
        binding_lookup_pairs: List[Tuple[str, str]] = list(targets)
        for m_obj in metas_map.values():
            if m_obj and m_obj.table_name:
                binding_lookup_pairs.append((m_obj.schema_name, m_obj.table_name))
        all_bindings_by_tbl = self.ctx.standard_catalog.get_tables_field_bindings_batch(
            prof.connection_id, binding_lookup_pairs
        )
        all_codes = [
            b.get("std_code", "")
            for tbl_map in all_bindings_by_tbl.values()
            for b in tbl_map.values()
            if (b.get("std_code") or "").strip()
        ]
        std_dict_map = self.ctx.standard_catalog.get_items_by_codes(all_codes)

        table_results: List[Dict[str, Any]] = []
        all_compliance_updates: List[Dict[str, Any]] = []
        schema_realignments: List[Tuple[str, str, str]] = []
        prune_targets: List[Tuple[str, str, List[str]]] = []
        agg = {
            "checked_tables": 0,
            "total_cols": 0,
            "bound_cols": 0,
            "compliant_cols": 0,
            "comment_only_cols": 0,
            "non_compliant_cols": 0,
            "std_missing_cols": 0,
            "unbound_cols": 0,
        }
        skipped_probe_tables = 0
        progress_step = 1 if total_targets <= 40 else (5 if total_targets <= 200 else 10)

        for idx, (sch, tbl) in enumerate(targets, start=1):
            req_k = (sch.lower(), tbl.lower())
            if req_k in skipped_keys:
                skipped_probe_tables += 1
                if idx == total_targets:
                    report(
                        96.5,
                        "核验库表落标状态",
                        f"[{idx}/{total_targets}] 核验 {sch}.{tbl} 落标状态...",
                        idx,
                        total_targets,
                    )
                continue
            t_meta = metas_map.get(req_k)
            if t_meta is None:
                skipped_probe_tables += 1
                if idx == total_targets:
                    report(
                        96.5,
                        "核验库表落标状态",
                        f"[{idx}/{total_targets}] 核验 {sch}.{tbl} 落标状态...",
                        idx,
                        total_targets,
                    )
                continue

            if idx == 1 or idx % progress_step == 0 or idx == total_targets:
                pct = probe_end_pct + (96.5 - probe_end_pct) * (idx / max(1, total_targets))
                report(
                    pct,
                    "核验库表落标状态",
                    f"[{idx}/{total_targets}] 核验 {sch}.{tbl} 落标状态...",
                    idx,
                    total_targets,
                )

            actual_s = t_meta.schema_name or sch
            actual_t = t_meta.table_name or tbl
            if t_meta.exists and t_meta.columns:
                prune_targets.append(
                    (actual_s, actual_t, [c.column_name for c in t_meta.columns if c.column_name])
                )
            should_realign = bool(
                unqualified_tables
                and req_k in unqualified_tables
                and t_meta.exists
                and actual_s.lower() != sch.lower()
            )
            if should_realign:
                schema_realignments.append((sch, actual_s, actual_t))
                if realign_replace_mode:
                    b_map = dict(all_bindings_by_tbl.get((sch.lower(), actual_t.lower())) or {})
                else:
                    b_map = dict(all_bindings_by_tbl.get((actual_s.lower(), actual_t.lower())) or {})
                    b_map.update(all_bindings_by_tbl.get((sch.lower(), actual_t.lower())) or {})
            else:
                b_map = {}
                if sch.lower() != actual_s.lower():
                    b_map.update(all_bindings_by_tbl.get((sch.lower(), actual_t.lower())) or {})
                b_map.update(all_bindings_by_tbl.get((actual_s.lower(), actual_t.lower())) or {})

            t_res, c_updates = self._evaluate_table_meta_compliance(
                prof=prof,
                eff_schema=sch,
                eff_table=tbl,
                t_meta=t_meta,
                bindings_map=b_map,
                std_dict_map=std_dict_map,
                format_profile=eff_format,
            )
            if c_updates:
                if should_realign:
                    for u in c_updates:
                        u["schema_name"] = actual_s
                        u["table_name"] = actual_t
                all_compliance_updates.extend(c_updates)
            table_results.append(t_res)

            s = t_res["summary"]
            agg["checked_tables"] += 1
            for k in (
                "total_cols",
                "bound_cols",
                "compliant_cols",
                "comment_only_cols",
                "non_compliant_cols",
                "std_missing_cols",
                "unbound_cols",
            ):
                agg[k] += int(s.get(k, 0) or 0)

        if schema_realignments:
            self.ctx.standard_catalog.realign_table_bindings_schema(
                prof.connection_id,
                schema_realignments,
                replace_mode=realign_replace_mode,
            )

        for p_sch, p_tbl, p_cols in prune_targets:
            self.ctx.standard_catalog.prune_stale_table_bindings(
                prof.connection_id, p_sch, p_tbl, p_cols
            )

        if all_compliance_updates:
            report(
                97.5,
                "同步核验结果",
                f"正在批量更新 {len(all_compliance_updates)} 个已绑字段的落标状态...",
                total_targets,
                total_targets,
            )
            self.ctx.standard_catalog.update_binding_compliance_batch(
                prof.connection_id, all_compliance_updates
            )

        agg["coverage_pct"] = (
            round(agg["bound_cols"] * 100.0 / agg["total_cols"], 1)
            if agg["total_cols"] > 0
            else 0.0
        )
        agg["compliant_pct"] = (
            round(agg["compliant_cols"] * 100.0 / agg["total_cols"], 1)
            if agg["total_cols"] > 0
            else 0.0
        )

        self.ctx.store.add_log(
            "INFO",
            "落标核验",
            f"已完成 [{prof.name}] 共 {agg['checked_tables']} 张表 {agg['total_cols']} 个字段落标核验（已落标 {agg['compliant_cols']} / 仅差注释 {agg['comment_only_cols']} / 未落标 {agg['non_compliant_cols']} / 未绑标 {agg['unbound_cols']}）",
        )
        table_stats = self.ctx.standard_catalog.get_connection_binding_stats(prof.connection_id)
        out: Dict[str, Any] = {
            "ok": True,
            "connection_id": prof.connection_id,
            "format_profile": eff_format,
            "aggregate": agg,
            "tables": table_results,
            "table_stats": table_stats,
            "message": f"核验完成：{agg['checked_tables']} 张表共 {agg['total_cols']} 列，已落标 {agg['compliant_cols']} 列、未落标 {agg['non_compliant_cols']} 列、仅差注释 {agg['comment_only_cols']} 列",
        }
        if probe_warning and skipped_probe_tables:
            out["warning"] = (
                f"数据源实时探查受限（已跳过 {skipped_probe_tables} 张未缓存表的在线拉取：{probe_warning}），对照绑定已成功入库"
            )
        if len(table_results) == 1:
            out["active_table"] = table_results[0]
        return out

    def generate_binding_template_bytes(
        self,
        *,
        connection_id: str = "",
        schema_name: str = "",
        tables: str = "",
        scope: str = "",
        progress_cb: ProgressCallback = noop_progress,
    ) -> bytes:
        """生成「数据标准-表字段对照关系」系统模板（支持空白模板、按选定库表或全库预填真实字段）."""
        report = progress_cb or noop_progress
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "字段与数据标准对照"

        headers = [
            "模式名",
            "表名",
            "物理字段名",
            "库内当前类型(参考)",
            "库内当前注释(参考)",
            "数据项编号",
            "标准英文简称(选填)",
            "标准中文名称(选填)",
        ]
        ws.append(headers)

        prefilled_rows: List[List[str]] = []
        if connection_id:
            prof = self.ctx.connection_hub.get_profile(connection_id)
            if prof:
                eff_def = prof.effective_default_schema()
                raw_list = [x.strip() for x in (tables or "").split(",") if x.strip()]
                targets = self._parse_table_targets(
                    tables=raw_list,
                    schema_name=schema_name,
                    default_schema=eff_def,
                )
                if not targets and (scope or "").strip().lower() == "all":
                    catalog = self.ctx.metadata_hub.get_catalog_tree(
                        prof.connection_id, auto_refresh=False
                    )
                    cat_tables = catalog.get("tables") or []
                    bound_tables = self.ctx.standard_catalog.list_bound_tables_for_connection(
                        prof.connection_id
                    )
                    if not cat_tables:
                        report(
                            8.0,
                            "采集全库目录",
                            f"数据源 [{prof.name}] 目录缓存为空，先触发全库目录采集...",
                            0,
                            1,
                        )
                        try:
                            catalog = self.ctx.metadata_hub.refresh_catalog(
                                prof.connection_id,
                                progress_cb=lambda pct, stg, msg, cur, tot: report(
                                    8.0 + 18.0 * (pct / 100.0), stg, msg, cur, tot
                                ),
                            )
                            cat_tables = catalog.get("tables") or []
                        except Exception:
                            if not bound_tables:
                                raise
                    seen_t: Set[Tuple[str, str]] = set()
                    for t in cat_tables:
                        s_c = clean_identifier_input(t.get("schema_name") or eff_def) or eff_def
                        t_c = clean_identifier_input(t.get("table_name") or "")
                        if t_c and (s_c.lower(), t_c.lower()) not in seen_t:
                            seen_t.add((s_c.lower(), t_c.lower()))
                            targets.append((s_c, t_c))
                    for s_b, t_b in bound_tables:
                        s_c = clean_identifier_input(s_b or eff_def) or eff_def
                        t_c = clean_identifier_input(t_b or "")
                        if t_c and (s_c.lower(), t_c.lower()) not in seen_t:
                            seen_t.add((s_c.lower(), t_c.lower()))
                            targets.append((s_c, t_c))

                if targets:
                    report(
                        28.0,
                        "探查表字段结构",
                        f"正在读取 {len(targets)} 张库表结构与已绑标准...",
                        0,
                        len(targets),
                    )
                    metas_map, _, _ = self.ctx.metadata_hub.get_tables_metadata_batch(
                        prof.connection_id,
                        targets,
                        force_refresh=False,
                        tolerate_errors=True,
                        on_progress=lambda done_c, tot_c, cs, ct: report(
                            28.0 + 52.0 * (min(done_c, max(1, tot_c)) / max(1, tot_c)),
                            "探查表字段结构",
                            f"正在读取库表结构 ({min(done_c + 1, tot_c)}/{tot_c}): {cs}.{ct}",
                            done_c,
                            tot_c,
                        ),
                    )
                    lookup_pairs = list(targets)
                    for m_obj in metas_map.values():
                        if m_obj and m_obj.table_name:
                            lookup_pairs.append((m_obj.schema_name, m_obj.table_name))
                            if (m_obj.schema_name or "").lower() != "public":
                                lookup_pairs.append(("public", m_obj.table_name))
                    binds_batch = self.ctx.standard_catalog.get_tables_field_bindings_batch(
                        prof.connection_id, lookup_pairs
                    )
                    all_codes = [
                        b.get("std_code", "")
                        for tbl_m in binds_batch.values()
                        for b in tbl_m.values()
                        if (b.get("std_code") or "").strip()
                    ]
                    std_map = self.ctx.standard_catalog.get_items_by_codes(all_codes)

                    seen_actual_tpl_tbls: Set[Tuple[str, str]] = set()
                    for sch, tbl in targets:
                        t_meta = metas_map.get((sch.lower(), tbl.lower()))
                        if t_meta is None:
                            continue
                        actual_s = t_meta.schema_name or sch
                        actual_t = t_meta.table_name or tbl
                        act_k = (actual_s.lower(), actual_t.lower())
                        if act_k in seen_actual_tpl_tbls:
                            continue
                        seen_actual_tpl_tbls.add(act_k)
                        binds: Dict[str, Dict[str, Any]] = {}
                        if act_k[0] != "public":
                            binds.update(binds_batch.get(("public", actual_t.lower())) or {})
                        if sch.lower() != act_k[0]:
                            binds.update(binds_batch.get((sch.lower(), actual_t.lower())) or {})
                        binds.update(binds_batch.get(act_k) or {})
                        for col in t_meta.columns:
                            col_clean = clean_identifier_input(col.column_name) or col.column_name
                            b = binds.get(col_clean.lower())
                            code = (b.get("std_code") if b else "") or ""
                            s_item = std_map.get(code) if code else None
                            prefilled_rows.append(
                                [
                                    actual_s,
                                    actual_t,
                                    col_clean,
                                    col.data_type or "",
                                    col.column_comment or "",
                                    code,
                                    s_item.get("en_abbr", "") if s_item else "",
                                    s_item.get("cn_name", "") if s_item else "",
                                ]
                            )
                    report(
                        92.0,
                        "写入预填模板",
                        f"已聚合 {len(seen_actual_tpl_tbls)} 张表共 {len(prefilled_rows)} 个字段，正在生成 Excel...",
                        len(seen_actual_tpl_tbls),
                        len(seen_actual_tpl_tbls),
                    )

        if prefilled_rows:
            for r in prefilled_rows:
                ws.append(r)
        else:
            for r in (
                [
                    "public",
                    "dwd_cust_info",
                    "cust_no",
                    "varchar(32)",
                    "客户号",
                    "CUST000101",
                    "CUST_ID",
                    "客户统一编号",
                ],
                [
                    "public",
                    "dwd_cust_info",
                    "open_dt",
                    "date",
                    "开户日期",
                    "ACCT000205",
                    "OPEN_DT",
                    "账户开户日期",
                ],
            ):
                ws.append(r)

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
        required_cols = {2, 3, 6}
        for c_i in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=c_i)
            cell.fill = req_fill if c_i in required_cols else opt_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[get_column_letter(c_i)].width = 22
        ws.row_dimensions[1].height = 26
        ws.freeze_panes = "A2"
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=len(headers)):
            for cell in row:
                cell.font = body_font
                cell.border = thin_border

        ws_note = wb.create_sheet(title="填写说明")
        ws_note.append(["列名称", "是否必填", "填写说明"])
        for n in (
            ["模式名", "选填", "留空时自动取当前数据源默认 Schema"],
            ["表名", "必填", "数据库表英文名"],
            ["物理字段名", "必填", "数据库当前物理字段英文名"],
            ["数据项编号", "优先必填", "填写第 1 步「标准字典」中的数据项编号；若留空，系统将按英文简称/中文名称自动匹配编号"],
            ["标准英文简称 / 中文名称", "辅助选填", "数据项编号为空时用于自动反查标准字典，或作为核对参考"],
        ):
            ws_note.append(n)
        for c_i in range(1, 4):
            c = ws_note.cell(row=1, column=c_i)
            c.fill = req_fill
            c.font = header_font
        ws_note.column_dimensions["A"].width = 24
        ws_note.column_dimensions["B"].width = 12
        ws_note.column_dimensions["C"].width = 68

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def upload_field_bindings_excel(
        self,
        *,
        filename: str,
        content: bytes,
        connection_id: str,
        default_schema: str = "",
        mode: str = "merge",
        format_profile: str = "AUTO",
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        report(
            14.0,
            "校验与读取工作簿",
            "正在校验文件结构并解析工作表（自动提取公式最终值、将 #N/A 置空）...",
            0,
            0,
        )
        prof = self.ctx.connection_hub.require_profile(connection_id)
        eff_format = self._resolve_format_profile(prof, format_profile)
        validate_xlsx_bytes(filename, content)

        # 自动清理历史遗留的 #N/A 等错误占位绑定，确保增量合并后状态干净准确
        self.ctx.standard_catalog.purge_invalid_error_bindings(prof.connection_id, force=True)

        def _norm(v: Any) -> str:
            return (
                clean_excel_cell_str(v)
                .lower()
                .replace("（", "(")
                .replace("）", ")")
                .replace(" ", "")
                .replace("_", "")
            )

        alias_map = {
            "schema_name": {"模式名", "schema", "schemaname", "所属模式"},
            "table_name": {"表名", "物理表名", "tablename", "表英文名"},
            "column_name": {"物理字段名", "字段名", "原字段名", "修改前字段名", "columnname", "列名"},
            "std_code": {
                "数据项编号",
                "标准编号",
                "数标编号",
                "数据标准编号",
                "数据项编码",
                "标准编码",
                "数据元编号",
                "数据元编码",
                "stdcode",
                "standardcode",
            },
            "std_en_abbr": {"标准英文简称(选填)", "标准英文简称", "英文简称", "标准字段名", "enabbr"},
            "std_cn_name": {"标准中文名称(选填)", "标准中文名称", "标准中文名", "中文名称", "cnname"},
        }

        matched_sheets: List[Tuple[str, int, Dict[str, int], List[List[Any]]]] = []
        skipped_non_empty_sheet = False

        with ResolvedWorkbook(content, read_only_preferred=True) as rwb:
            for sh_title in rwb.sheetnames:
                t_norm = (sh_title or "").strip().lower()
                if t_norm in (
                    "填写说明",
                    "填表说明",
                    "使用说明",
                    "说明",
                    "目录",
                    "封面",
                    "readme",
                    "instructions",
                ):
                    continue
                probe_rows = list(rwb.iter_sheet_rows(sh_title, max_rows=15))
                if not probe_rows:
                    continue
                skipped_non_empty_sheet = True
                found_h_idx = -1
                found_col_map: Dict[str, int] = {}
                for r_idx, row in enumerate(probe_rows):
                    normed = [_norm(c) for c in row]
                    candidate: Dict[str, int] = {}
                    for k, aliases in alias_map.items():
                        for c_i, cell_t in enumerate(normed):
                            if cell_t in aliases and k not in candidate:
                                candidate[k] = c_i
                                break
                    if (
                        "table_name" in candidate
                        and "column_name" in candidate
                        and any(k in candidate for k in ("std_code", "std_en_abbr", "std_cn_name"))
                    ):
                        found_h_idx = r_idx
                        found_col_map = candidate
                        break
                if found_h_idx != -1:
                    full_rows = rwb.extract_sheet_rows_with_merged(
                        sh_title,
                        row_progress_cb=lambda done_r, tot_r, _st=sh_title: report(
                            min(36.0, 16.0 + 20.0 * done_r / max(1, tot_r)),
                            "读取工作表并计算公式最终值",
                            f"正在解析 [{_st}] 第 {done_r} / {max(done_r, tot_r)} 行（自动计算公式与清洗 #N/A）...",
                            done_r,
                            max(done_r, tot_r),
                        ),
                    )
                    if len(full_rows) >= found_h_idx + 2:
                        matched_sheets.append((sh_title, found_h_idx, found_col_map, full_rows))

        if not matched_sheets:
            if not skipped_non_empty_sheet:
                raise ValueError("对照关系表为空，请下载系统模板后填写再上传")
            raise ValueError(
                "未识别到有效对照表头（需包含「表名」「物理字段名」「数据项编号」等列），请下载系统模板对照"
            )

        explicit_default_sch = clean_identifier_input(default_schema)
        eff_default_schema = explicit_default_sch or prof.effective_default_schema()
        eff_mode = (mode or "merge").strip().lower()

        cached_cat = self.ctx.store.get_cached_catalog(prof.connection_id)
        cached_schemas_by_tbl: Dict[str, List[str]] = {}
        for ct in cached_cat.get("tables") or []:
            ct_name = str(ct.get("table_name") or "").strip().lower()
            ct_sch = str(ct.get("schema_name") or "").strip()
            if ct_name and ct_sch:
                lst_s = cached_schemas_by_tbl.setdefault(ct_name, [])
                if ct_sch not in lst_s:
                    lst_s.append(ct_sch)

        parsed_bindings: List[Dict[str, Any]] = []
        unresolved_rows: List[str] = []
        touched_tables: Dict[Tuple[str, str], Tuple[str, str]] = {}
        unqualified_tables: Set[Tuple[str, str]] = set()
        explicit_schema_tables: Set[Tuple[str, str]] = set()

        total_data_rows = sum(max(0, len(s_rows) - (h_i + 1)) for _, h_i, _, s_rows in matched_sheets)
        scanned_rows = 0
        staged_rows: List[Tuple[int, str, str, str, str, str, str]] = []
        pending_cand_queries: List[Tuple[str, str]] = []

        for sh_title, header_idx, col_map, rows in matched_sheets:
            for row_no, row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
                scanned_rows += 1
                if scanned_rows == 1 or scanned_rows % 100 == 0 or scanned_rows == total_data_rows:
                    pct = 36.0 + (15.0 * scanned_rows / max(1, total_data_rows))
                    report(
                        pct,
                        "识别并匹配数据标准",
                        f"正在扫描对照行 {scanned_rows} / {total_data_rows}...",
                        scanned_rows,
                        total_data_rows,
                    )

                if not row or all(clean_excel_cell_str(x) == "" for x in row):
                    continue

                def _cell(k: str, *, placeholder_empty: bool = False) -> str:
                    ci = col_map.get(k)
                    if ci is None or ci >= len(row):
                        return ""
                    return clean_excel_cell_str(
                        row[ci], treat_placeholder_empty=placeholder_empty
                    )

                raw_sch_cell = _cell("schema_name", placeholder_empty=True)
                raw_tbl_cell = _cell("table_name", placeholder_empty=True)
                has_explicit_sch = bool(clean_identifier_input(raw_sch_cell))
                sch = clean_identifier_input(raw_sch_cell) or eff_default_schema
                tbl = clean_identifier_input(raw_tbl_cell)
                col = clean_identifier_input(_cell("column_name", placeholder_empty=True))
                if "." in tbl and not raw_sch_cell:
                    p_sch, p_tbl = tbl.split(".", 1)
                    cleaned_ps = clean_identifier_input(p_sch)
                    if cleaned_ps:
                        has_explicit_sch = True
                        sch = cleaned_ps
                    tbl = clean_identifier_input(p_tbl)
                if not tbl or not col:
                    continue
                if _norm(tbl) in ("表名", "物理表名", "tablename") and _norm(col) in (
                    "物理字段名",
                    "字段名",
                    "columnname",
                ):
                    continue

                if not has_explicit_sch and not explicit_default_sch:
                    known_schs = cached_schemas_by_tbl.get(tbl.lower())
                    if known_schs:
                        exact_def = next(
                            (ks for ks in known_schs if ks.lower() == eff_default_schema.lower()),
                            None,
                        )
                        sch = exact_def or known_schs[0]
                    else:
                        unqualified_tables.add((sch.lower(), tbl.lower()))
                else:
                    explicit_schema_tables.add((sch.lower(), tbl.lower()))

                tbl_norm_key = (sch.lower(), tbl.lower())
                canon_sch, canon_tbl = touched_tables.setdefault(tbl_norm_key, (sch, tbl))

                raw_code = _cell("std_code", placeholder_empty=True)
                std_en = clean_identifier_input(_cell("std_en_abbr", placeholder_empty=True))
                std_cn = _cell("std_cn_name", placeholder_empty=True)
                if not raw_code and (std_en or std_cn):
                    pending_cand_queries.append((std_en, std_cn))
                staged_rows.append((row_no, canon_sch, canon_tbl, col, raw_code, std_en, std_cn))

        cand_batch_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        if pending_cand_queries:
            report(
                52.0,
                "识别并匹配数据标准",
                f"正在批量匹配未填编号的标准名称（共 {len(pending_cand_queries)} 行候选）...",
                scanned_rows,
                max(1, total_data_rows),
            )
            cand_batch_map = self.ctx.standard_catalog.match_standard_candidates_batch(
                pending_cand_queries
            )

        for idx_sr, (row_no, canon_sch, canon_tbl, col, raw_code, std_en, std_cn) in enumerate(
            staged_rows, start=1
        ):
            matched_code = raw_code
            if not matched_code and (std_en or std_cn):
                cands = cand_batch_map.get((std_en.lower(), std_cn)) or []
                if cands:
                    picked_cand = cands[0]
                    if not std_en and col:
                        for c_item in cands:
                            if (
                                clean_identifier_input(c_item.get("en_abbr") or "").lower()
                                == col.lower()
                            ):
                                picked_cand = c_item
                                break
                    matched_code = picked_cand["std_code"]

            if not matched_code:
                unresolved_rows.append(f"第{row_no}行 {canon_sch}.{canon_tbl}.{col}")
            else:
                parsed_bindings.append(
                    {
                        "schema_name": canon_sch,
                        "table_name": canon_tbl,
                        "column_name": col,
                        "std_code": matched_code,
                    }
                )
            if idx_sr == 1 or idx_sr % 200 == 0 or idx_sr == len(staged_rows):
                pct = 53.0 + (6.0 * idx_sr / max(1, len(staged_rows)))
                report(
                    pct,
                    "识别并匹配数据标准",
                    f"已识别有效绑定 {len(parsed_bindings)} 条（空值/#N/A 跳过 {len(unresolved_rows)} 行）",
                    idx_sr,
                    max(1, len(staged_rows)),
                )

        if not parsed_bindings and not (eff_mode == "replace_table" and touched_tables):
            if unresolved_rows:
                raise ValueError(
                    f"共扫描 {len(unresolved_rows)} 行字段，但数据项编号均为空值（含 #N/A）且未匹配到标准，未产生有效对照绑定"
                )
            raise ValueError("未解析到包含有效数据项编号或匹配标准的对照行")

        canon_tables = list(touched_tables.values())
        report(
            60.0,
            "保存对照关系",
            f"正在持久化 {len(parsed_bindings)} 条字段标准绑定（涉及 {len(canon_tables)} 张表）...",
            0,
            max(1, len(parsed_bindings)),
        )
        self.ctx.standard_catalog.bulk_save_field_bindings(
            prof.connection_id,
            parsed_bindings,
            mode=eff_mode,
            replace_tables=canon_tables if eff_mode == "replace_table" else None,
            bind_source="EXCEL",
            progress_cb=report,
            progress_start_pct=60.0,
            progress_end_pct=70.0,
        )
        report(
            70.0,
            "核验库表落标状态",
            f"已完成 {len(parsed_bindings)} 条绑定落盘提交，正在加载 {len(canon_tables)} 张库表结构...",
            len(parsed_bindings),
            max(1, len(parsed_bindings)),
        )

        targets = [{"schema_name": sch, "table_name": tbl} for sch, tbl in canon_tables]
        eff_unqualified = unqualified_tables - explicit_schema_tables
        check_res = self.check_compliance(
            connection_id=prof.connection_id,
            tables=targets,
            format_profile=eff_format,
            force_refresh=False,
            progress_cb=report,
            tolerate_probe_errors=True,
            purge_errors=False,
            progress_start_pct=70.0,
            unqualified_tables=eff_unqualified if eff_unqualified else None,
            realign_replace_mode=(eff_mode == "replace_table"),
        )
        msg = f"成功导入 {len(parsed_bindings)} 条字段标准对照关系（涉及 {len(canon_tables)} 张表）"
        if unresolved_rows:
            msg += f"，另有 {len(unresolved_rows)} 行空值（含 #N/A）按未绑标跳过"
        check_res["imported_count"] = len(parsed_bindings)
        check_res["unresolved_count"] = len(unresolved_rows)
        check_res["message"] = msg
        return check_res

    def stage_migration_from_bindings(
        self,
        *,
        connection_id: str,
        schema_name: str,
        table_name: str,
        column_names: Optional[List[str]] = None,
        format_profile: str = "AUTO",
        append: bool = True,
    ) -> Dict[str, Any]:
        """将已绑定数标的未达标字段纳入全局贯标队列，全内存生成 DDL 与策略判定（零远端库重连）."""
        prof = self.ctx.connection_hub.require_profile(connection_id)
        eff_format = self._resolve_format_profile(prof, format_profile)
        comp, t_meta = self._get_table_compliance_with_meta(
            connection_id,
            schema_name,
            table_name,
            format_profile=eff_format,
            force_refresh=False,
            include_table_stats=False,
            purge_errors=False,
        )
        t_info = comp["table"]
        actual_sch = t_info["schema_name"]
        actual_tbl = t_info["table_name"]

        selected_set = {
            clean_identifier_input(c).lower() for c in (column_names or []) if str(c or "").strip()
        }
        new_items: List[Dict[str, Any]] = []
        for col in comp["columns"]:
            if not col.get("std_code") or col.get("compliance_status") == "STD_MISSING":
                continue
            if selected_set:
                if col["column_name"].lower() not in selected_set:
                    continue
            elif col["compliance_status"] == "COMPLIANT":
                continue

            target_name = (
                col["column_name"] if col.get("name_ok") else (col["std_en_abbr"] or col["column_name"])
            )
            target_type = (
                col["data_type"] if col.get("type_ok") else (col["std_target_type"] or col["data_type"])
            )
            target_comment = col["std_cn_name"] or col["column_comment"]

            new_items.append(
                {
                    "row_index": len(new_items) + 1,
                    "connection_id": prof.connection_id,
                    "schema_name": actual_sch,
                    "table_name": actual_tbl,
                    "old_col_name": col["column_name"],
                    "old_col_type": col["data_type"],
                    "old_col_comment": col["column_comment"],
                    "new_col_name": target_name,
                    "new_col_type": target_type,
                    "new_col_comment": target_comment,
                }
            )

        if not new_items:
            raise ValueError("所选字段暂无需要贯标改造的已绑标条目（或均已达标）")

        eng, _ = self._get_engine_for_connection(prof.connection_id)
        eng_meta = self._to_engine_table_meta(t_meta) if t_meta is not None else None
        if eng_meta is not None:
            claimed_old_names = {
                clean_identifier_input(str(x.get("old_col_name") or "")).lower()
                for x in new_items
            }
            for idx, it in enumerate(new_items):
                tmp_item = dict(it)
                tmp_item["id"] = idx + 1
                upd = eng._build_single_item_sync_update(
                    tmp_item,
                    eng_meta,
                    claimed_old_names=claimed_old_names,
                    dialect=prof.db_type,
                )
                for k in (
                    "status",
                    "strategy",
                    "status_detail",
                    "ordinal_position",
                    "total_columns",
                    "db_current_col_name",
                    "db_current_type",
                    "db_current_comment",
                    "is_distribute_key",
                    "table_orientation",
                    "generated_sql",
                ):
                    if k in upd:
                        it[k] = upd[k]

        if not self.ctx.store.get_meta("migrator_connection_id", ""):
            self.ctx.store.save_meta("migrator_connection_id", prof.connection_id)

        label = f"数标落标生成 [{actual_sch}.{actual_tbl}]"
        if append:
            self.ctx.store.upsert_migration_items(
                new_items, label, connection_id=prof.connection_id
            )
        else:
            for idx, it in enumerate(new_items, start=1):
                it["row_index"] = idx
            self.ctx.store.replace_all_items(new_items, label, preserve_history=True)

        state = self.get_workspace_state()
        state["staged_count"] = len(new_items)
        state["message"] = (
            f"已将 {actual_sch}.{actual_tbl} 的 {len(new_items)} 个字段纳入全局贯标队列（当前共 {len(state['items'])} 条）"
        )
        return state


def actual_len_text(items: List[Any]) -> str:
    return str(len(items or []))
