"""跨环境比对模块 — 已保存映射表对检索、元数据预热与闭环推送混入类."""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from core.platform.dialects import clean_identifier_input
from core.platform.import_progress import ProgressCallback, noop_progress


class _CrossSearchAndPushMixin:
    """映射表对搜索、批量预热与推送至贯标队列."""

    def search_saved_pairs(self, keyword: str = "", limit: int = 200) -> Dict[str, Any]:
        """按表名、映射字段名或缓存库表物理字段/注释检索已保存映射表对."""
        kw = (keyword or "").strip()
        kw_low = kw.lower()
        max_limit = max(1, min(500, int(limit or 200)))

        src_prof = self._resolve_role_profile("source")
        tgt_prof = self._resolve_role_profile("target")
        src_cid = src_prof.connection_id if src_prof else ""
        tgt_cid = tgt_prof.connection_id if tgt_prof else ""
        src_def_sch = src_prof.effective_default_schema() if src_prof else "public"
        tgt_def_sch = tgt_prof.effective_default_schema() if tgt_prof else "public"

        with self.ctx.store.module_db() as conn:
            t_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM cross_table_bindings ORDER BY id ASC"
                ).fetchall()
            ]
            if not t_rows:
                return {"ok": True, "keyword": kw, "total": 0, "items": []}

            col_counts: Dict[str, int] = {}
            saved_cols_by_key: Dict[str, List[Dict[str, str]]] = {}
            for r in conn.execute(
                """
                SELECT std_table_key, orig_col_name, std_col_name, std_col_comment,
                       bound_source_col, bound_target_col
                FROM cross_column_bindings
                ORDER BY row_index ASC, id ASC
                """
            ).fetchall():
                t_key = str(r["std_table_key"] or "").strip().lower()
                if not t_key:
                    continue
                col_counts[t_key] = col_counts.get(t_key, 0) + 1
                if kw_low:
                    saved_cols_by_key.setdefault(t_key, []).append(
                        {
                            "bound_source_col": str(r["bound_source_col"] or ""),
                            "bound_target_col": str(r["bound_target_col"] or ""),
                            "orig_col_name": str(r["orig_col_name"] or ""),
                            "std_col_name": str(r["std_col_name"] or ""),
                            "std_col_comment": str(r["std_col_comment"] or ""),
                        }
                    )

            meta_hits: Dict[Tuple[str, str, str], List[str]] = {}
            fsb_hits: Dict[Tuple[str, str, str], List[str]] = {}
            if kw_low and (src_cid or tgt_cid):
                like_pat = f"%{kw_low}%"
                cid_list = list(dict.fromkeys(c for c in (src_cid, tgt_cid) if c))
                ph = ",".join("?" for _ in cid_list)
                for mr in conn.execute(
                    f"""
                    SELECT connection_id, schema_name, table_name, column_name, column_comment
                    FROM meta_columns
                    WHERE connection_id IN ({ph})
                      AND (LOWER(column_name) LIKE ? OR LOWER(column_comment) LIKE ?)
                    ORDER BY ordinal_position ASC
                    """,
                    [*cid_list, like_pat, like_pat],
                ).fetchall():
                    mk = (
                        str(mr["connection_id"] or ""),
                        clean_identifier_input(str(mr["schema_name"] or "")).lower(),
                        clean_identifier_input(str(mr["table_name"] or "")).lower(),
                    )
                    cname = str(mr["column_name"] or "").strip()
                    if cname:
                        meta_hits.setdefault(mk, []).append(cname)

                for fr in conn.execute(
                    f"""
                    SELECT connection_id, schema_name, table_name, column_name, std_code
                    FROM field_standard_bindings
                    WHERE connection_id IN ({ph})
                      AND (LOWER(column_name) LIKE ? OR LOWER(std_code) LIKE ?)
                    ORDER BY id ASC
                    """,
                    [*cid_list, like_pat, like_pat],
                ).fetchall():
                    fk = (
                        str(fr["connection_id"] or ""),
                        clean_identifier_input(str(fr["schema_name"] or "")).lower(),
                        clean_identifier_input(str(fr["table_name"] or "")).lower(),
                    )
                    cname = str(fr["column_name"] or "").strip()
                    if cname:
                        fsb_hits.setdefault(fk, []).append(cname)

        def _append_unique(dst: List[str], seen_set: Set[str], val: str) -> None:
            v = clean_identifier_input(val) or val.strip()
            if not v:
                return
            vl = v.lower()
            if vl not in seen_set:
                seen_set.add(vl)
                dst.append(v)

        def _lookup_side_hits(
            cid: str, raw_sch: str, raw_tbl: str, def_sch: str, is_sqlite: bool
        ) -> List[str]:
            if not cid or not raw_tbl:
                return []
            tbl_low = clean_identifier_input(raw_tbl).lower()
            sch_low = clean_identifier_input(raw_sch).lower()
            sch_candidates: List[str] = []
            if is_sqlite:
                sch_candidates = ["main"] if not sch_low or sch_low == "public" else [sch_low, "main"]
            else:
                if sch_low:
                    sch_candidates.append(sch_low)
                if (not sch_low or sch_low == "public") and def_sch.lower() not in sch_candidates:
                    sch_candidates.append(def_sch.lower())
            hits: List[str] = []
            for sl in sch_candidates:
                hits.extend(meta_hits.get((cid, sl, tbl_low), ()))
                hits.extend(fsb_hits.get((cid, sl, tbl_low), ()))
            return hits

        matched_table_first: List[Dict[str, Any]] = []
        matched_col_only: List[Dict[str, Any]] = []

        for t in t_rows:
            t_key = str(t.get("std_table_key") or "").strip().lower()
            s_sch = str(t.get("source_schema") or "").strip()
            s_tbl = str(t.get("source_table") or t.get("std_table_name") or "").strip()
            t_sch = str(t.get("target_schema") or "").strip()
            t_tbl = str(t.get("target_table") or t.get("std_table_name") or "").strip()
            col_cnt = col_counts.get(t_key, 0)

            if not kw_low:
                matched_table_first.append(
                    {
                        "std_table_key": t_key,
                        "source_schema": s_sch,
                        "source_table": s_tbl,
                        "target_schema": t_sch,
                        "target_table": t_tbl,
                        "std_table_name": str(t.get("std_table_name") or ""),
                        "column_count": col_cnt,
                        "match_scope": "all",
                        "matched_source_cols": [],
                        "matched_target_cols": [],
                        "matched_source_count": 0,
                        "matched_target_count": 0,
                    }
                )
                continue

            tbl_fields = (
                t_key,
                s_sch.lower(),
                s_tbl.lower(),
                t_sch.lower(),
                t_tbl.lower(),
                str(t.get("std_table_name") or "").lower(),
                f"{s_sch}.{s_tbl}".lower(),
                f"{t_sch}.{t_tbl}".lower(),
                f"{s_tbl} ↔ {t_tbl}".lower(),
            )
            tbl_hit = any(kw_low in f for f in tbl_fields if f)

            m_src: List[str] = []
            m_tgt: List[str] = []
            seen_src_l: Set[str] = set()
            seen_tgt_l: Set[str] = set()

            for c in saved_cols_by_key.get(t_key, ()):
                sc_nm = c["bound_source_col"]
                tc_nm = c["bound_target_col"]
                orig_nm = c["orig_col_name"]
                std_nm = "" if c["std_col_name"].startswith("__unpaired_") else c["std_col_name"]
                std_cm = c["std_col_comment"]
                if sc_nm and (kw_low in sc_nm.lower() or (std_cm and kw_low in std_cm.lower())):
                    _append_unique(m_src, seen_src_l, sc_nm)
                elif not sc_nm and orig_nm and kw_low in orig_nm.lower() and not tc_nm:
                    _append_unique(m_src, seen_src_l, orig_nm)

                if tc_nm and (
                    kw_low in tc_nm.lower()
                    or (std_nm and kw_low in std_nm.lower())
                    or (std_cm and kw_low in std_cm.lower())
                ):
                    _append_unique(m_tgt, seen_tgt_l, tc_nm)

            for hit_c in _lookup_side_hits(
                src_cid,
                s_sch or src_def_sch,
                s_tbl,
                src_def_sch,
                bool(src_prof and src_prof.db_type == "SQLITE"),
            ):
                _append_unique(m_src, seen_src_l, hit_c)

            for hit_c in _lookup_side_hits(
                tgt_cid,
                t_sch or tgt_def_sch,
                t_tbl,
                tgt_def_sch,
                bool(tgt_prof and tgt_prof.db_type == "SQLITE"),
            ):
                _append_unique(m_tgt, seen_tgt_l, hit_c)

            col_hit = bool(m_src or m_tgt)
            if not tbl_hit and not col_hit:
                continue

            item = {
                "std_table_key": t_key,
                "source_schema": s_sch,
                "source_table": s_tbl,
                "target_schema": t_sch,
                "target_table": t_tbl,
                "std_table_name": str(t.get("std_table_name") or ""),
                "column_count": col_cnt,
                "match_scope": "both" if (tbl_hit and col_hit) else ("table" if tbl_hit else "column"),
                "matched_source_cols": m_src[:4],
                "matched_target_cols": m_tgt[:4],
                "matched_source_count": len(m_src),
                "matched_target_count": len(m_tgt),
            }
            if tbl_hit:
                matched_table_first.append(item)
            else:
                matched_col_only.append(item)

        combined = matched_table_first + matched_col_only
        return {
            "ok": True,
            "keyword": kw,
            "total": len(combined),
            "items": combined[:max_limit],
        }

    def prefetch_saved_pairs(
        self,
        *,
        max_pairs: int = 60,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        """后台并发预热已保存映射表对的源/目标端元数据缓存."""
        report = progress_cb or noop_progress
        limit = max(1, int(max_pairs or 60))
        src_conn = self.get_role_connection_brief("source")
        tgt_conn = self.get_role_connection_brief("target")
        tables = self.ctx.store.list_cross_tables()[:limit]

        src_cid = str(src_conn.get("connection_id") or "") if src_conn.get("configured") else ""
        tgt_cid = str(tgt_conn.get("connection_id") or "") if tgt_conn.get("configured") else ""
        src_def_sch = str(src_conn.get("default_schema") or "public")
        tgt_def_sch = str(tgt_conn.get("default_schema") or "public")

        def _collect_targets(sch_key: str, tbl_key: str, def_sch: str) -> List[Tuple[str, str]]:
            out: List[Tuple[str, str]] = []
            seen: set[Tuple[str, str]] = set()
            for t in tables:
                raw_t = str(t.get(tbl_key) or "").strip()
                if not raw_t:
                    continue
                raw_s = str(t.get(sch_key) or def_sch).strip() or def_sch
                if "." in raw_t and not str(t.get(sch_key) or "").strip():
                    raw_s, raw_t = raw_t.split(".", 1)
                k = (clean_identifier_input(raw_s).lower(), clean_identifier_input(raw_t).lower())
                if k[1] and k not in seen:
                    seen.add(k)
                    out.append((raw_s, raw_t))
            return out

        src_targets = _collect_targets("source_schema", "source_table", src_def_sch) if src_cid else []
        tgt_targets = _collect_targets("target_schema", "target_table", tgt_def_sch) if tgt_cid else []
        total_targets = len(src_targets) + len(tgt_targets)

        if total_targets == 0:
            report(95.0, "预热完成", "暂无需要预热的已保存映射表对", 0, 0)
            return {
                "ok": True,
                "prefetched_source": 0,
                "prefetched_target": 0,
                "warnings": [],
            }

        report(
            15.0,
            "预热元数据缓存",
            f"正在并发预热来源端 {len(src_targets)} 表与目标端 {len(tgt_targets)} 表...",
            0,
            total_targets,
        )
        progress_lock = threading.Lock()
        done_by_side: Dict[str, int] = {"source": 0, "target": 0}
        warnings: List[str] = []

        def _run_side(side: str, cid: str, targets: List[Tuple[str, str]]) -> int:
            side_label = "来源端" if side == "source" else "目标端"

            def _on_prog(done_n: int, tot_n: int, cur_s: str, cur_t: str) -> None:
                with progress_lock:
                    done_by_side[side] = min(len(targets), max(done_by_side[side], int(done_n)))
                    proc = done_by_side["source"] + done_by_side["target"]
                    pct = min(95.0, 18.0 + 77.0 * proc / max(1, total_targets))
                    report(
                        pct,
                        "预热元数据缓存",
                        f"正在预热{side_label} {cur_s}.{cur_t} ({done_n}/{tot_n})",
                        proc,
                        total_targets,
                    )

            try:
                out_map, warn, skipped = self.ctx.metadata_hub.get_tables_metadata_batch(
                    cid,
                    targets,
                    force_refresh=False,
                    penetrate_views=False,
                    tolerate_errors=True,
                    on_progress=_on_prog,
                )
                if warn:
                    with progress_lock:
                        warnings.append(f"{side_label}: {warn}")
                resolved_cnt = sum(
                    1
                    for s, t in targets
                    if (
                        clean_identifier_input(s).lower(),
                        clean_identifier_input(t).lower(),
                    )
                    in out_map
                )
                return resolved_cnt if resolved_cnt > 0 else max(0, len(targets) - len(skipped))
            except Exception as exc:
                with progress_lock:
                    warnings.append(f"{side_label}: {exc}")
                return 0

        jobs: Dict[str, Tuple[str, List[Tuple[str, str]]]] = {}
        if src_cid and src_targets:
            jobs["source"] = (src_cid, src_targets)
        if tgt_cid and tgt_targets:
            jobs["target"] = (tgt_cid, tgt_targets)

        prefetched_source = 0
        prefetched_target = 0
        if len(jobs) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_map = {
                    side: pool.submit(_run_side, side, cid, tgts)
                    for side, (cid, tgts) in jobs.items()
                }
                if "source" in fut_map:
                    prefetched_source = fut_map["source"].result()
                if "target" in fut_map:
                    prefetched_target = fut_map["target"].result()
        else:
            for side, (cid, tgts) in jobs.items():
                cnt = _run_side(side, cid, tgts)
                if side == "source":
                    prefetched_source = cnt
                else:
                    prefetched_target = cnt

        return {
            "ok": True,
            "prefetched_source": prefetched_source,
            "prefetched_target": prefetched_target,
            "warnings": warnings,
        }

    def sync_standards_from_migrator(self) -> Dict[str, Any]:
        wb_items = self.ctx.store.list_items()
        if not wb_items:
            raise ValueError("当前『单库贯标执行台』中尚无清单记录，请先上传落标清单或数标文件")
        fn = self.ctx.store.get_meta("current_excel_name", "执行台同步清单.xlsx")
        self.ctx.store.replace_cross_standards(wb_items, fn, preserve_bindings=True)
        return self.get_overview_state()

    def push_to_migrator(
        self,
        std_table_key: Optional[str] = None,
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        """经由 PlatformStateStore 数据契约将待贯标字段推送至单库贯标队列（零跨模块导入）."""
        report = progress_cb or noop_progress
        tables = (
            [self.ctx.store.get_cross_table_binding(std_table_key)]
            if std_table_key
            else self.ctx.store.list_cross_tables()
        )
        tables = [t for t in tables if t]
        if not tables:
            raise ValueError("当前没有可推送的标准绑定表")

        report(
            16.0,
            "加载映射表对",
            f"准备抽取 {len(tables)} 张表的待贯标规则...",
            0,
            len(tables),
        )
        src_conn_info = self.get_role_connection_brief("source")
        tgt_conn_info = self.get_role_connection_brief("target")
        tgt_cid = str(
            self.ctx.store.get_meta("cross_target_connection_id", "")
            or tgt_conn_info.get("connection_id")
            or ""
        )
        tgt_db_type = str(tgt_conn_info.get("db_type") or "DWS")
        eff_tgt_fmt = self._resolve_side_format_profile(
            tgt_conn_info, target_format_profile, format_profile
        )
        items_to_push: List[Dict[str, Any]] = []
        row_idx = 1

        total_tbls = len(tables)
        for idx_t, t in enumerate(tables, start=1):
            t_key = t["std_table_key"]
            low_p = 20.0 + 62.0 * (idx_t - 1) / max(1, total_tbls)
            high_p = 20.0 + 62.0 * idx_t / max(1, total_tbls)
            report(
                low_p,
                "解析贯标规则",
                f"正在对齐 [{t_key}] 目标表与数据标准 ({idx_t}/{total_tbls})...",
                idx_t - 1,
                total_tbls,
            )

            def _tbl_cb(
                pct: float,
                stg: str,
                dtl: str = "",
                proc: int = 0,
                tot: int = 0,
                _low: float = low_p,
                _high: float = high_p,
            ) -> None:
                mapped = _low + (_high - _low) * max(0.0, min(100.0, float(pct or 0.0))) / 100.0
                report(mapped, stg, dtl, idx_t - 1, total_tbls)

            comp = self.compare_single_table(
                t_key,
                format_profile=format_profile,
                source_format_profile=source_format_profile,
                target_format_profile=eff_tgt_fmt,
                src_conn=src_conn_info,
                tgt_conn=tgt_conn_info,
                progress_cb=_tbl_cb if report is not noop_progress else noop_progress,
            )
            tgt_panel = comp.get("target_panel") or {}
            tgt_schema = (
                tgt_panel.get("schema_name")
                or t.get("target_schema")
                or t.get("std_schema_name")
                or "public"
            )
            tgt_table = (
                tgt_panel.get("table_name")
                or t.get("target_table")
                or t["std_table_name"]
            )

            dual_rows = comp.get("dual_rows") or []
            dict_pushed_for_table = False

            # 优先使用双列拟物书签绑定的数据标准字典记录构建精准贯标改造项
            for dr in dual_rows:
                tc = dr.get("target_col")
                t_code = (dr.get("target_std_code") or "").strip()
                if not tc or not t_code:
                    continue
                std_item = self.ctx.standard_catalog.get_item(t_code)
                if not std_item:
                    continue
                dict_pushed_for_table = True
                resolved_type = self.ctx.standard_catalog.resolve_physical_type(
                    std_item, db_type=tgt_db_type, format_profile=eff_tgt_fmt
                )
                old_c = clean_identifier_input(tc.get("column_name") or "")
                new_c = clean_identifier_input(std_item.get("en_abbr") or old_c)
                items_to_push.append(
                    {
                        "row_index": row_idx,
                        "connection_id": tgt_cid,
                        "schema_name": tgt_schema,
                        "table_name": tgt_table,
                        "old_col_name": old_c,
                        "old_col_type": tc.get("data_type") or "",
                        "old_col_comment": tc.get("column_comment") or "",
                        "new_col_name": new_c,
                        "new_col_type": resolved_type or tc.get("data_type") or "",
                        "new_col_comment": std_item.get("cn_name") or "",
                    }
                )
                row_idx += 1

            if dict_pushed_for_table:
                continue

            saved_cross_cols = self.ctx.store.list_cross_columns(t_key)
            has_dict_paired_rows = any(
                (c.get("source_std_code") or "").strip()
                or (c.get("target_std_code") or "").strip()
                for c in saved_cross_cols
            )
            has_legacy_checklist_rows = any(
                not str(c.get("std_col_name") or "").startswith("__unpaired_")
                and (
                    (c.get("std_col_type") or "").strip()
                    or (c.get("orig_col_type") or "").strip()
                    or (c.get("std_col_comment") or "").strip()
                )
                for c in saved_cross_cols
            )
            if has_dict_paired_rows or not has_legacy_checklist_rows:
                continue

            cols = (comp.get("standard_panel") or {}).get("columns") or saved_cross_cols
            for c in cols:
                t_col_meta = c.get("target_col_meta")
                if isinstance(t_col_meta, dict) and t_col_meta.get("column_name"):
                    old_c = clean_identifier_input(t_col_meta["column_name"])
                else:
                    old_c = clean_identifier_input(
                        c.get("bound_target_col")
                        if c.get("bound_target_col") and c.get("bound_target_col") != c.get("std_col_name")
                        else (c.get("orig_col_name") or c.get("bound_source_col") or c["std_col_name"])
                    )
                new_c = clean_identifier_input(
                    c.get("std_col_name") or c.get("bound_target_col") or old_c
                )
                items_to_push.append(
                    {
                        "row_index": row_idx,
                        "connection_id": tgt_cid,
                        "schema_name": tgt_schema,
                        "table_name": tgt_table,
                        "old_col_name": old_c,
                        "old_col_type": (
                            (t_col_meta.get("data_type") if isinstance(t_col_meta, dict) else "")
                            or c.get("orig_col_type")
                            or c.get("std_col_type")
                            or ""
                        ),
                        "old_col_comment": (
                            (t_col_meta.get("column_comment") if isinstance(t_col_meta, dict) else "")
                            or c.get("orig_col_comment")
                            or ""
                        ),
                        "new_col_name": new_c,
                        "new_col_type": c.get("std_col_type") or c.get("orig_col_type") or "",
                        "new_col_comment": c.get("std_col_comment") or "",
                    }
                )
                row_idx += 1

        if not items_to_push:
            raise ValueError("当前标准表暂无可推送的字段改造规则，请先导入数标或连接来源/目标库加载字段")

        report(
            86.0,
            "写入贯标队列",
            f"正在推送 {len(items_to_push)} 条贯标规则至单库贯标执行台...",
            len(items_to_push),
            len(items_to_push),
        )
        fn = self.ctx.store.get_meta("cross_standard_filename", "跨环境数标推送清单.xlsx")
        self.ctx.store.upsert_migration_items(items_to_push, fn, connection_id=tgt_cid)

        # 若已绑定目标库，将贯标执行台的激活连接同步指向该目标库
        if tgt_cid:
            self.ctx.store.save_meta("migrator_connection_id", tgt_cid)

        self.ctx.store.add_log(
            "INFO",
            "跨模块联动",
            f"已从三栏比对中心推送 {len(tables)} 张表共 {len(items_to_push)} 条数标规则至单库贯标执行台",
        )
        return {
            "ok": True,
            "pushed_count": len(items_to_push),
            "table_count": len(tables),
            "target_connection_id": tgt_cid,
            "message": f"已将 {len(tables)} 张表共 {len(items_to_push)} 条数标规则推送至『单库贯标执行台』！",
        }
