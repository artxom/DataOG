"""单库落标贯标模块 — 库级离线 HTML / 单 Sheet Excel 报告混入类."""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from core.dws_engine import item_requires_table_rebuild
from core.platform.dialects import clean_identifier_input
from core.platform.import_progress import ProgressCallback, noop_progress
from core.platform.offline_report import (
    build_single_db_excel_bytes,
    build_single_db_html_bytes,
)


class _StandardReportMixin:
    """全库级落标与贯标汇总报告生成与导出."""

    def build_database_compliance_report_data(
        self,
        *,
        connection_id: str = "",
        format_profile: str = "AUTO",
        tables: str = "",
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        """构建单库全量（库级）落标核验与贯标队列汇总报告数据."""
        report = progress_cb or noop_progress
        prof = self._resolve_binding_profile(connection_id)
        if not prof:
            raise ValueError("请先绑定目标数据源连接")
        eff_format = self._resolve_format_profile(prof, format_profile)
        fmt_map = {
            p["value"]: p["label"]
            for p in self.ctx.standard_catalog.list_format_profiles()
        }
        fmt_label = fmt_map.get(eff_format, eff_format)

        eff_def = prof.effective_default_schema()
        raw_list = [x.strip() for x in (tables or "").split(",") if x.strip()]
        targets = self._parse_table_targets(
            tables=raw_list,
            default_schema=eff_def,
        )

        warnings: List[str] = []
        cat_meta_by_key: Dict[Tuple[str, str], Dict[str, str]] = {}
        catalog_auto_refreshed = False

        catalog = self.ctx.metadata_hub.get_catalog_tree(prof.connection_id, auto_refresh=False)
        cat_tables = catalog.get("tables") or []
        for t in cat_tables:
            s_c = clean_identifier_input(t.get("schema_name") or eff_def) or eff_def
            t_c = clean_identifier_input(t.get("table_name") or "")
            if t_c:
                cat_meta_by_key[(s_c.lower(), t_c.lower())] = {
                    "object_type": str(t.get("object_type") or "TABLE"),
                    "table_comment": str(t.get("table_comment") or ""),
                }

        def _norm_probe_schema(raw_s: str) -> str:
            cs = clean_identifier_input(raw_s or "") or eff_def
            if (
                cs.lower() == "public"
                and eff_def.lower() != "public"
                and prof.db_type
                in (
                    "SQLITE",
                    "INCEPTOR",
                    "MYSQL",
                    "MARIADB",
                    "OCEANBASE",
                    "DORIS",
                    "STARROCKS",
                    "TIDB",
                )
            ):
                return eff_def
            if prof.db_type == "SQLITE":
                return "main"
            return cs

        if not targets:
            bound_tables = self.ctx.standard_catalog.list_bound_tables_for_connection(
                prof.connection_id
            )
            if not cat_tables:
                report(
                    5.0,
                    "采集全库目录",
                    f"数据源 [{prof.name}] 目录缓存为空，先触发全库目录采集...",
                    0,
                    1,
                )
                try:
                    catalog = self.ctx.metadata_hub.refresh_catalog(
                        prof.connection_id,
                        progress_cb=lambda pct, stg, msg, cur, tot: report(
                            5.0 + 11.0 * (pct / 100.0), stg, msg, cur, tot
                        ),
                    )
                    catalog_auto_refreshed = True
                except Exception as exc:
                    if not bound_tables:
                        raise ValueError(f"自动采集库目录失败，无法生成库级报告：{exc}") from exc
                    warnings.append(f"自动采集库目录未完成（已回退使用本地已绑标表清单）：{exc}")
                else:
                    cat_tables = catalog.get("tables") or []
                    for t in cat_tables:
                        s_c = clean_identifier_input(t.get("schema_name") or eff_def) or eff_def
                        t_c = clean_identifier_input(t.get("table_name") or "")
                        if t_c:
                            cat_meta_by_key[(s_c.lower(), t_c.lower())] = {
                                "object_type": str(t.get("object_type") or "TABLE"),
                                "table_comment": str(t.get("table_comment") or ""),
                            }

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

        if not targets:
            raise ValueError(f"数据源 [{prof.name}] 暂无可导出的库表或字段绑标记录")

        batch_res = self.check_compliance(
            connection_id=prof.connection_id,
            tables=[{"schema_name": s, "table_name": t} for s, t in targets],
            format_profile=eff_format,
            force_refresh=False,
            progress_cb=report,
            tolerate_probe_errors=True,
            purge_errors=True,
            progress_start_pct=18.0,
        )
        if batch_res.get("warning"):
            warnings.append(str(batch_res["warning"]))

        checked_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for item in batch_res.get("tables") or []:
            t_info = item.get("table") or {}
            k = (
                str(t_info.get("schema_name") or "").lower(),
                str(t_info.get("table_name") or "").lower(),
            )
            prev_item = checked_by_key.get(k)
            if prev_item is None or (
                int((item.get("summary") or {}).get("bound_cols") or 0)
                >= int((prev_item.get("summary") or {}).get("bound_cols") or 0)
            ):
                checked_by_key[k] = item

        mig_conn_id = str(self.ctx.store.get_meta("migrator_connection_id", "") or "")
        raw_mig = self.ctx.store.list_items()
        target_key_set = {(s.lower(), t.lower()) for s, t in targets}
        mig_items = []
        for it in raw_mig:
            item_cid = str(it.get("connection_id") or "")
            if item_cid == prof.connection_id:
                mig_items.append(it)
            elif not item_cid:
                if mig_conn_id == prof.connection_id:
                    mig_items.append(it)
                elif not mig_conn_id and (
                    str(it.get("schema_name") or "").lower(),
                    str(it.get("table_name") or "").lower(),
                ) in target_key_set:
                    mig_items.append(it)

        queue_by_col: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for it in mig_items:
            s_l = str(it.get("schema_name") or "").lower()
            t_l = str(it.get("table_name") or "").lower()
            primary_k, secondary_k = (
                ("new_col_name", "old_col_name")
                if it.get("status") == "COMPLETED"
                else ("old_col_name", "new_col_name")
            )
            p_cn = str(it.get(primary_k) or "").strip().lower()
            if p_cn:
                queue_by_col.setdefault((s_l, t_l, p_cn), it)
        for it in mig_items:
            s_l = str(it.get("schema_name") or "").lower()
            t_l = str(it.get("table_name") or "").lower()
            for cname_k in ("old_col_name", "new_col_name"):
                cn = str(it.get(cname_k) or "").strip().lower()
                if cn:
                    queue_by_col.setdefault((s_l, t_l, cn), it)

        enriched_tables: List[Dict[str, Any]] = []
        emitted_actual_keys: Set[Tuple[str, str]] = set()
        bound_tables_count = 0
        full_compliant_tables_count = 0
        agg_counts = {
            "total_cols": 0,
            "bound_cols": 0,
            "unbound_cols": 0,
            "compliant_cols": 0,
            "comment_only_cols": 0,
            "non_compliant_cols": 0,
            "std_missing_cols": 0,
        }

        for sch, tbl in targets:
            req_k = (sch.lower(), tbl.lower())
            norm_k = (_norm_probe_schema(sch).lower(), tbl.lower())
            checked = checked_by_key.get(req_k) or checked_by_key.get(norm_k)
            cat_info = cat_meta_by_key.get(req_k) or cat_meta_by_key.get(norm_k) or {}
            if checked is None:
                if req_k in emitted_actual_keys or norm_k in emitted_actual_keys:
                    continue
                emitted_actual_keys.add(req_k)
                enriched_tables.append(
                    {
                        "table": {
                            "schema_name": sch,
                            "table_name": tbl,
                            "object_type": cat_info.get("object_type", "TABLE"),
                            "table_comment": cat_info.get("table_comment", ""),
                        },
                        "summary": {
                            "total_cols": 0,
                            "bound_cols": 0,
                            "compliant_cols": 0,
                            "comment_only_cols": 0,
                            "non_compliant_cols": 0,
                            "std_missing_cols": 0,
                            "unbound_cols": 0,
                            "coverage_pct": 0.0,
                            "compliant_pct": 0.0,
                        },
                        "columns": [],
                        "probe_error": str(
                            batch_res.get("warning") or "表结构在线探查受限，暂未获取到列定义"
                        ),
                    }
                )
                continue

            t_info = dict(checked.get("table") or {})
            act_s_l = str(t_info.get("schema_name") or sch).lower()
            act_t_l = str(t_info.get("table_name") or tbl).lower()
            if (act_s_l, act_t_l) in emitted_actual_keys:
                continue
            emitted_actual_keys.add((act_s_l, act_t_l))

            if not t_info.get("object_type") and cat_info.get("object_type"):
                t_info["object_type"] = cat_info["object_type"]
            if not t_info.get("table_comment") and cat_info.get("table_comment"):
                t_info["table_comment"] = cat_info["table_comment"]

            cols_out: List[Dict[str, Any]] = []
            for c in checked.get("columns") or []:
                c_copy = dict(c)
                c_name_l = str(c_copy.get("column_name") or "").lower()
                q_it = queue_by_col.get((act_s_l, act_t_l, c_name_l)) or queue_by_col.get(
                    (sch.lower(), tbl.lower(), c_name_l)
                )
                if q_it:
                    c_copy["queue_status"] = str(q_it.get("status") or "")
                    c_copy["queue_strategy"] = (
                        "TABLE_REBUILD" if item_requires_table_rebuild(q_it) else "DIRECT_ALTER"
                    )
                    c_copy["queue_item_id"] = q_it.get("id")
                cols_out.append(c_copy)

            s = dict(checked.get("summary") or {})
            if int(s.get("bound_cols") or 0) > 0:
                bound_tables_count += 1
            if int(s.get("total_cols") or 0) > 0 and int(s.get("compliant_cols") or 0) >= int(
                s.get("total_cols") or 0
            ):
                full_compliant_tables_count += 1
            for k in agg_counts:
                agg_counts[k] += int(s.get(k) or 0)

            enriched_tables.append(
                {
                    "table": t_info,
                    "summary": s,
                    "columns": cols_out,
                }
            )

        tot_c = agg_counts["total_cols"]
        queue_completed = sum(1 for it in mig_items if it.get("status") == "COMPLETED")
        queue_pending = len(mig_items) - queue_completed

        report(
            98.5,
            "装配报告文件",
            f"已完成 {len(enriched_tables)} 张库表（{tot_c} 字段）聚合，正在生成报告文件...",
            len(enriched_tables),
            len(enriched_tables),
        )

        return {
            "report_type": "single_db",
            "catalog_auto_refreshed": catalog_auto_refreshed,
            "connection": {
                "connection_id": prof.connection_id,
                "name": prof.name,
                "db_type": prof.db_type,
                "host": prof.host,
                "port": prof.port,
                "dbname": prof.dbname or prof.sqlite_path or "",
                "default_schema": eff_def,
            },
            "format_profile": eff_format,
            "format_profile_label": fmt_label,
            "aggregate": {
                "total_tables": len(enriched_tables),
                "bound_tables_count": bound_tables_count,
                "full_compliant_tables_count": full_compliant_tables_count,
                **agg_counts,
                "coverage_pct": round(agg_counts["bound_cols"] * 100.0 / tot_c, 1) if tot_c else 0.0,
                "compliant_pct": round(agg_counts["compliant_cols"] * 100.0 / tot_c, 1)
                if tot_c
                else 0.0,
                "queue_total": len(mig_items),
                "queue_pending": queue_pending,
                "queue_completed": queue_completed,
            },
            "tables": enriched_tables,
            "migration_items": mig_items,
            "warnings": warnings,
        }

    def export_binding_report_bytes(
        self,
        *,
        connection_id: str = "",
        format_profile: str = "AUTO",
        tables: str = "",
        progress_cb: ProgressCallback = noop_progress,
    ) -> bytes:
        report_data = self.build_database_compliance_report_data(
            connection_id=connection_id,
            format_profile=format_profile,
            tables=tables,
            progress_cb=progress_cb,
        )
        return build_single_db_excel_bytes(report_data)

    def export_binding_report_html_bytes(
        self,
        *,
        connection_id: str = "",
        format_profile: str = "AUTO",
        tables: str = "",
        progress_cb: ProgressCallback = noop_progress,
    ) -> bytes:
        report_data = self.build_database_compliance_report_data(
            connection_id=connection_id,
            format_profile=format_profile,
            tables=tables,
            progress_cb=progress_cb,
        )
        return build_single_db_html_bytes(report_data)
