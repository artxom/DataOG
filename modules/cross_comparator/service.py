"""16:9 跨环境三栏数标比对中心服务 (CrossComparatorService).

严格遵循插件隔离规范：
1. 仅依赖 `core.platform` 公共底座，严禁导入任何其他 `modules.*` 私有模块；
2. 全面接入 `ConnectionHub` 与 `MetadataHub`，左栏（来源库）与右栏（目标库）支持任意内置原生驱动与自定义 JDBC 驱动数据源；
3. 推送待贯标改造项至执行台一律通过 `PlatformStateStore` 状态存储契约完成。
"""

from __future__ import annotations

import concurrent.futures
import inspect
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from core.platform.context import PlatformContext
from core.platform.dialects import (
    analyze_cross_column_compatibility,
    clean_identifier_input,
    is_cross_type_compatible,
    normalize_cross_type,
)
from core.platform.excel_io import clean_excel_cell_str
from core.platform.import_progress import ProgressCallback, noop_progress
from core.platform.models import ColumnMeta, TableMeta
from modules.cross_comparator.io_and_report_mixin import _CrossIoAndReportMixin
from modules.cross_comparator.search_and_push_mixin import _CrossSearchAndPushMixin


def _supports_kwarg(fn: Any, kw: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    if kw in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())



class CrossComparatorService(_CrossSearchAndPushMixin, _CrossIoAndReportMixin):
    """跨环境三栏数标比对中心核心服务."""

    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def _meta_key_for_role(self, role: str) -> str:
        r = (role or "").strip().lower()
        if r not in ("source", "target"):
            raise ValueError("环境角色必须为 source (来源库) 或 target (目标库)")
        return f"cross_{r}_connection_id"

    def _resolve_role_profile(self, role: str):
        meta_key = self._meta_key_for_role(role)
        cid = str(self.ctx.store.get_meta(meta_key, "") or "")
        prof = self.ctx.connection_hub.get_profile(cid) if cid else None
        if prof is None and role == "target":
            # 若目标库未单独绑定，默认复用单库贯标执行台已绑定的连接
            fallback_cid = str(self.ctx.store.get_meta("migrator_connection_id", "") or "")
            if fallback_cid:
                prof = self.ctx.connection_hub.get_profile(fallback_cid)
                if prof:
                    self.ctx.store.save_meta(meta_key, prof.connection_id)
        return prof

    def get_role_connection_brief(
        self, role: str, *, connection_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """读取角色连接轻量摘要（不扫描全库目录树）."""
        if connection_id:
            self._meta_key_for_role(role)
            prof = self.ctx.connection_hub.get_profile(connection_id)
        else:
            prof = self._resolve_role_profile(role)

        if prof is None:
            return {
                "role": role,
                "configured": False,
                "connection_id": "",
                "name": "",
                "db_type": "DWS",
                "driver_track": "NATIVE",
                "host": "",
                "port": 8000,
                "dbname": "",
                "schema_name": "public",
                "default_schema": "public",
                "effective_default_schema": "public",
                "user": "",
                "standard_format": "AUTO",
            }

        pub = prof.to_public_dict()
        return {
            **pub,
            "role": role,
            "configured": True,
            "schema_name": prof.default_schema,
            "effective_default_schema": prof.effective_default_schema(),
        }

    def get_role_connection_info(self, role: str) -> Dict[str, Any]:
        brief = self.get_role_connection_brief(role)
        if not brief.get("configured"):
            return {
                **brief,
                "schemas": [],
                "tables": [],
            }

        cat = self.ctx.metadata_hub.get_catalog_tree(
            brief["connection_id"], auto_refresh=False
        )
        return {
            **brief,
            "schemas": [
                s["schema_name"] if isinstance(s, dict) else str(s)
                for s in cat["schemas"]
            ],
            "tables": cat["tables"],
        }

    @staticmethod
    def _resolve_side_format_profile(
        conn_info: Optional[Dict[str, Any]],
        explicit_side: str = "AUTO",
        fallback_single: str = "AUTO",
    ) -> str:
        exp = (explicit_side or "").strip()
        if exp and exp != "AUTO":
            return exp
        fb = (fallback_single or "").strip()
        if fb and fb != "AUTO":
            return fb
        saved = str((conn_info or {}).get("standard_format") or "").strip()
        if saved:
            return saved
        return "AUTO"

    def bind_role_connection(
        self,
        role: str,
        connection_id: str,
        *,
        active_table_key: str = "",
        auto_refresh: bool = False,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        meta_key = self._meta_key_for_role(role)
        side_label = "来源库 (左栏)" if (role or "").strip().lower() == "source" else "目标库 (右栏)"
        if not connection_id:
            report(30.0, "解绑数据源", f"正在清除{side_label}连接绑定...", 0, 0)
            self.ctx.store.save_meta(meta_key, "")
            return self.get_overview_state(
                selected_table_key=active_table_key, progress_cb=report
            )

        report(18.0, "校验连接档案", f"正在绑定{side_label}连接...", 0, 0)
        prof = self.ctx.connection_hub.require_profile(connection_id)
        self.ctx.store.save_meta(meta_key, prof.connection_id)
        warning = ""
        if auto_refresh:
            report(30.0, "刷新目录缓存", f"正在采集 [{prof.name}] 库表目录...", 0, 0)

            def _cat_cb(
                pct: float, stg: str, dtl: str = "", proc: int = 0, tot: int = 0
            ) -> None:
                mapped = 28.0 + 0.37 * max(0.0, min(100.0, float(pct or 0.0)))
                report(mapped, stg or "刷新目录缓存", dtl, proc, tot)

            try:
                refresh_fn = self.ctx.metadata_hub.refresh_catalog
                if report is not noop_progress and _supports_kwarg(refresh_fn, "progress_cb"):
                    refresh_fn(prof.connection_id, progress_cb=_cat_cb)
                else:
                    refresh_fn(prof.connection_id)
            except Exception as exc:
                warning = f"已绑定连接 [{prof.name}]，但自动刷新目录缓存未完成: {exc}"

        report(70.0, "刷新比对视图", f"正在加载 [{prof.name}] 表对视图...", 0, 0)
        state = self.get_overview_state(
            selected_table_key=active_table_key, progress_cb=report
        )
        if warning:
            state["warning"] = warning
        state["message"] = f"已将 [{prof.name}] 绑定为{side_label}"
        return state

    @staticmethod
    def _find_matching_table_key(
        tables: List[Dict[str, Any]],
        *,
        source_schema: Optional[str] = None,
        source_table: Optional[str] = None,
        target_schema: Optional[str] = None,
        target_table: Optional[str] = None,
    ) -> str:
        """根据 (source_table, target_table) 精确查找已保存的跨环境映射表键."""
        def _split_tbl(raw_sch: Optional[str], raw_tbl: Optional[str]) -> Tuple[str, str]:
            sch = clean_identifier_input(raw_sch or "").lower()
            txt = (raw_tbl or "").strip()
            if "." in txt:
                p0, p1 = txt.split(".", 1)
                if not sch:
                    sch = clean_identifier_input(p0).lower()
                txt = p1
            return sch, clean_identifier_input(txt).lower()

        def _clean_tbl_low(raw: Optional[str]) -> str:
            txt = (raw or "").strip()
            if "." in txt:
                txt = txt.split(".", 1)[1]
            return clean_identifier_input(txt).lower()

        def _sch_compat(q_sch: str, saved_sch_raw: Any) -> bool:
            saved_sch = clean_identifier_input(str(saved_sch_raw or "")).lower()
            return not q_sch or not saved_sch or saved_sch == q_sch

        s_sch, s_tbl = _split_tbl(source_schema, source_table)
        t_sch, t_tbl = _split_tbl(target_schema, target_table)

        if s_tbl and t_tbl:
            # 1) 优先同时匹配表名与模式名
            for t in tables:
                if (
                    _clean_tbl_low(t.get("source_table")) == s_tbl
                    and _clean_tbl_low(t.get("target_table")) == t_tbl
                    and (not s_sch or clean_identifier_input(t.get("source_schema") or "").lower() == s_sch)
                    and (not t_sch or clean_identifier_input(t.get("target_schema") or "").lower() == t_sch)
                ):
                    return str(t["std_table_key"])
            # 2) 次优匹配：仅当查询侧或已保存侧的模式名为空时回退匹配双方表名（禁止跨模式错配）
            for t in tables:
                if (
                    _clean_tbl_low(t.get("source_table")) == s_tbl
                    and _clean_tbl_low(t.get("target_table")) == t_tbl
                    and _sch_compat(s_sch, t.get("source_schema"))
                    and _sch_compat(t_sch, t.get("target_schema"))
                ):
                    return str(t["std_table_key"])
            return ""

        if t_tbl:
            for t in tables:
                if (
                    _clean_tbl_low(t.get("target_table")) == t_tbl
                    or str(t.get("std_table_key") or "").strip().lower() == t_tbl
                ) and _sch_compat(t_sch, t.get("target_schema")):
                    return str(t["std_table_key"])
        elif s_tbl:
            for t in tables:
                if (
                    _clean_tbl_low(t.get("source_table")) == s_tbl
                    and _sch_compat(s_sch, t.get("source_schema"))
                ):
                    return str(t["std_table_key"])
        return ""

    def _resolve_comparison_target(
        self,
        *,
        selected_table_key: Optional[str] = None,
        source_schema: Optional[str] = None,
        source_table: Optional[str] = None,
        target_schema: Optional[str] = None,
        target_table: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], str, str, bool]:
        tables = self.ctx.store.list_cross_tables()
        if not tables and self.ctx.store.list_items():
            fn = self.ctx.store.get_meta("current_excel_name", "执行台同步清单.xlsx")
            tables = self.ctx.store.replace_cross_standards(
                self.ctx.store.list_items(), fn, preserve_bindings=True
            )

        active_key = (selected_table_key or "").strip().lower()
        if not active_key and tables and not (source_table or target_table):
            active_key = tables[0]["std_table_key"]

        lookup_key = ""
        load_saved_binding = False
        if active_key or source_table or target_table:
            lookup_key = active_key
            load_saved_binding = bool(active_key)
            if not lookup_key:
                matched_key = self._find_matching_table_key(
                    tables,
                    source_schema=source_schema,
                    source_table=source_table,
                    target_schema=target_schema,
                    target_table=target_table,
                )
                if matched_key:
                    active_key = matched_key
                    lookup_key = matched_key
                    load_saved_binding = True
                else:
                    raw_fb = (target_table or source_table or "").strip()
                    if "." in raw_fb:
                        raw_fb = raw_fb.split(".", 1)[1]
                    lookup_key = clean_identifier_input(raw_fb).lower()
                    load_saved_binding = False
        return tables, active_key, lookup_key, load_saved_binding

    def get_overview_state(
        self,
        selected_table_key: Optional[str] = None,
        force_refresh: bool = False,
        source_schema: Optional[str] = None,
        source_table: Optional[str] = None,
        target_schema: Optional[str] = None,
        target_table: Optional[str] = None,
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        report(10.0, "加载配置与连接", "正在读取跨环境连接与已保存表对...", 0, 0)
        tables, active_key, lookup_key, load_saved_binding = self._resolve_comparison_target(
            selected_table_key=selected_table_key,
            source_schema=source_schema,
            source_table=source_table,
            target_schema=target_schema,
            target_table=target_table,
        )

        src_conn = self.get_role_connection_info("source")
        tgt_conn = self.get_role_connection_info("target")
        eff_src_fmt = self._resolve_side_format_profile(
            src_conn, source_format_profile, format_profile
        )
        eff_tgt_fmt = self._resolve_side_format_profile(
            tgt_conn, target_format_profile, format_profile
        )

        comp = None
        if lookup_key:
            comp = self.compare_single_table(
                lookup_key,
                override_source_schema=source_schema,
                override_source_table=source_table,
                override_target_schema=target_schema,
                override_target_table=target_table,
                force_refresh=force_refresh,
                format_profile=format_profile,
                source_format_profile=eff_src_fmt,
                target_format_profile=eff_tgt_fmt,
                persist_missing_binding=bool(active_key),
                load_saved_binding=load_saved_binding,
                src_conn=src_conn,
                tgt_conn=tgt_conn,
                progress_cb=report,
            )
        else:
            report(92.0, "构建比对视图", "跨环境配置加载完成", 0, 0)

        return {
            "ok": True,
            "connections": self.ctx.connection_hub.list_profiles(),
            "source_connection": src_conn,
            "target_connection": tgt_conn,
            "format_profiles": self.ctx.standard_catalog.list_format_profiles(),
            "format_profile": format_profile or "AUTO",
            "source_format_profile": eff_src_fmt,
            "target_format_profile": eff_tgt_fmt,
            "dict_summary": self.ctx.standard_catalog.get_summary(),
            "standard_meta": {
                "filename": self.ctx.store.get_meta("cross_standard_filename", "未导入"),
                "imported_at": self.ctx.store.get_meta("cross_standard_imported_at", ""),
                "table_count": len(tables),
                "column_count": self.ctx.store.count_cross_columns(),
            },
            "tables": tables,
            "active_table_key": active_key,
            "active_comparison": comp,
        }

    def compare_pair_lightweight(
        self,
        selected_table_key: Optional[str] = None,
        force_refresh: bool = False,
        source_schema: Optional[str] = None,
        source_table: Optional[str] = None,
        target_schema: Optional[str] = None,
        target_table: Optional[str] = None,
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
        source_connection_id: Optional[str] = None,
        target_connection_id: Optional[str] = None,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        """表对切换轻量接口：零全库目录扫描与零全表字段扫描."""
        report = progress_cb or noop_progress
        report(10.0, "加载表对", "正在读取目标表对与连接摘要...", 0, 0)
        tables, active_key, lookup_key, load_saved_binding = self._resolve_comparison_target(
            selected_table_key=selected_table_key,
            source_schema=source_schema,
            source_table=source_table,
            target_schema=target_schema,
            target_table=target_table,
        )

        src_conn = self.get_role_connection_brief(
            "source", connection_id=source_connection_id or None
        )
        tgt_conn = self.get_role_connection_brief(
            "target", connection_id=target_connection_id or None
        )
        eff_src_fmt = self._resolve_side_format_profile(
            src_conn, source_format_profile, format_profile
        )
        eff_tgt_fmt = self._resolve_side_format_profile(
            tgt_conn, target_format_profile, format_profile
        )

        comp = None
        if lookup_key:
            comp = self.compare_single_table(
                lookup_key,
                override_source_schema=source_schema,
                override_source_table=source_table,
                override_target_schema=target_schema,
                override_target_table=target_table,
                force_refresh=force_refresh,
                format_profile=format_profile,
                source_format_profile=eff_src_fmt,
                target_format_profile=eff_tgt_fmt,
                persist_missing_binding=bool(active_key),
                load_saved_binding=load_saved_binding,
                src_conn=src_conn,
                tgt_conn=tgt_conn,
                progress_cb=report,
            )
        else:
            report(92.0, "构建比对视图", "表对切换视图就绪", 0, 0)

        return {
            "ok": True,
            "tables": tables,
            "active_table_key": active_key,
            "active_comparison": comp,
            "format_profile": format_profile or "AUTO",
            "source_format_profile": eff_src_fmt,
            "target_format_profile": eff_tgt_fmt,
            "dict_summary": self.ctx.standard_catalog.get_summary(),
            "source_connection": src_conn,
            "target_connection": tgt_conn,
        }

    def compare_single_table(
        self,
        std_table_key: str,
        *,
        override_source_schema: Optional[str] = None,
        override_source_table: Optional[str] = None,
        override_target_schema: Optional[str] = None,
        override_target_table: Optional[str] = None,
        force_refresh: bool = False,
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
        persist_missing_binding: bool = True,
        load_saved_binding: bool = True,
        src_conn: Optional[Dict[str, Any]] = None,
        tgt_conn: Optional[Dict[str, Any]] = None,
        prefetched_src_metas: Optional[Dict[Tuple[str, str], TableMeta]] = None,
        prefetched_tgt_metas: Optional[Dict[Tuple[str, str], TableMeta]] = None,
        prefetched_src_skipped: Optional[Set[Tuple[str, str]]] = None,
        prefetched_tgt_skipped: Optional[Set[Tuple[str, str]]] = None,
        prefetched_src_warning: str = "",
        prefetched_tgt_warning: str = "",
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        report(18.0, "准备比对", "正在解析表对与标准格式...", 0, 0)

        def _split_override(
            raw_sch: Optional[str], raw_tbl: Optional[str]
        ) -> Tuple[Optional[str], Optional[str]]:
            sch = clean_identifier_input(raw_sch or "") or None
            tbl_txt = (raw_tbl or "").strip()
            if "." in tbl_txt:
                p0, p1 = tbl_txt.split(".", 1)
                if not sch:
                    sch = clean_identifier_input(p0) or None
                tbl_txt = p1
            tbl = clean_identifier_input(tbl_txt) or None
            return sch, tbl

        override_source_schema, override_source_table = _split_override(
            override_source_schema, override_source_table
        )
        override_target_schema, override_target_table = _split_override(
            override_target_schema, override_target_table
        )

        t_bind = (
            self.ctx.store.get_cross_table_binding(std_table_key)
            if load_saved_binding
            else None
        )
        if not t_bind:
            if persist_missing_binding:
                t_bind = self.ctx.store.upsert_cross_table_binding(
                    std_table_key,
                    source_schema=override_source_schema or "public",
                    source_table=override_source_table or std_table_key,
                    target_schema=override_target_schema or "public",
                    target_table=override_target_table or std_table_key,
                )
            else:
                t_bind = {
                    "id": 0,
                    "std_table_key": std_table_key,
                    "std_schema_name": override_source_schema or override_target_schema or "public",
                    "std_table_name": override_source_table or override_target_table or std_table_key,
                    "std_table_comment": "",
                    "source_schema": override_source_schema or "",
                    "source_table": override_source_table or "",
                    "target_schema": override_target_schema or "",
                    "target_table": override_target_table or "",
                }

        src_conn = src_conn if src_conn is not None else self.get_role_connection_brief("source")
        tgt_conn = tgt_conn if tgt_conn is not None else self.get_role_connection_brief("target")
        eff_src_fmt = self._resolve_side_format_profile(
            src_conn, source_format_profile, format_profile
        )
        eff_tgt_fmt = self._resolve_side_format_profile(
            tgt_conn, target_format_profile, format_profile
        )

        src_schema = (
            override_source_schema
            or t_bind.get("source_schema")
            or src_conn.get("default_schema")
            or "public"
        )
        src_table = (
            override_source_table
            or t_bind.get("source_table")
            or t_bind["std_table_name"]
        )
        tgt_schema = (
            override_target_schema
            or t_bind.get("target_schema")
            or tgt_conn.get("default_schema")
            or "public"
        )
        tgt_table = (
            override_target_table
            or t_bind.get("target_table")
            or t_bind["std_table_name"]
        )

        src_meta = TableMeta(schema_name=src_schema, table_name=src_table, exists=False)
        tgt_meta = TableMeta(schema_name=tgt_schema, table_name=tgt_table, exists=False)
        src_error = ""
        tgt_error = ""

        has_src = bool(src_conn.get("configured") and src_conn.get("connection_id"))
        has_tgt = bool(tgt_conn.get("configured") and tgt_conn.get("connection_id"))
        probe_lock = threading.Lock()

        def _make_side_probe_cb(side_label: str) -> ProgressCallback:
            if report is noop_progress:
                return noop_progress

            def _side_cb(
                pct: float,
                stg: str,
                dtl: str = "",
                proc: int = 0,
                tot: int = 0,
            ) -> None:
                mapped_pct = 22.0 + 0.46 * max(0.0, min(100.0, float(pct or 0.0)))
                eff_stg = stg if "穿透" in str(stg) else "探查表结构"
                eff_dtl = f"{side_label}: {dtl or stg}"
                with probe_lock:
                    report(mapped_pct, eff_stg, eff_dtl, proc, tot)

            return _side_cb

        def _probe_side(
            conn_id: str,
            schema_name: str,
            table_name: str,
            side_label: str,
        ) -> TableMeta:
            fn = self.ctx.metadata_hub.get_table_metadata
            side_cb = _make_side_probe_cb(side_label)
            if side_cb is not noop_progress and _supports_kwarg(fn, "progress_cb"):
                return fn(
                    conn_id,
                    schema_name,
                    table_name,
                    force_refresh=force_refresh,
                    penetrate_views=True,
                    progress_cb=side_cb,
                )
            return fn(
                conn_id,
                schema_name,
                table_name,
                force_refresh=force_refresh,
                penetrate_views=True,
            )

        def _lookup_prefetched(
            metas_dict: Optional[Dict[Tuple[str, str], TableMeta]],
            skipped_set: Optional[Set[Tuple[str, str]]],
            warning_txt: str,
            schema_name: str,
            table_name: str,
        ) -> Tuple[bool, Optional[TableMeta], str]:
            if metas_dict is None and skipped_set is None:
                return False, None, ""
            k = (schema_name.lower(), table_name.lower())
            if metas_dict is not None and k in metas_dict:
                return True, metas_dict[k], ""
            if skipped_set is not None and k in skipped_set:
                return True, TableMeta(schema_name=schema_name, table_name=table_name, exists=False), (
                    warning_txt or f"表 {schema_name}.{table_name} 探查受限"
                )
            return False, None, ""

        src_pref_hit, src_pref_meta, src_pref_err = (
            _lookup_prefetched(
                prefetched_src_metas,
                prefetched_src_skipped,
                prefetched_src_warning,
                src_schema,
                src_table,
            )
            if has_src
            else (True, src_meta, "")
        )
        tgt_pref_hit, tgt_pref_meta, tgt_pref_err = (
            _lookup_prefetched(
                prefetched_tgt_metas,
                prefetched_tgt_skipped,
                prefetched_tgt_warning,
                tgt_schema,
                tgt_table,
            )
            if has_tgt
            else (True, tgt_meta, "")
        )

        if has_src and src_pref_hit and src_pref_meta is not None:
            src_meta = src_pref_meta
            src_error = src_pref_err
        if has_tgt and tgt_pref_hit and tgt_pref_meta is not None:
            tgt_meta = tgt_pref_meta
            tgt_error = tgt_pref_err

        need_probe_src = has_src and not src_pref_hit
        need_probe_tgt = has_tgt and not tgt_pref_hit

        if need_probe_src and need_probe_tgt:
            report(
                22.0,
                "探查表结构",
                f"并行探查来源表({src_schema}.{src_table})与目标表({tgt_schema}.{tgt_table})结构...",
                0,
                2,
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                src_fut = pool.submit(
                    _probe_side,
                    src_conn["connection_id"],
                    src_schema,
                    src_table,
                    "来源表",
                )
                tgt_fut = pool.submit(
                    _probe_side,
                    tgt_conn["connection_id"],
                    tgt_schema,
                    tgt_table,
                    "目标表",
                )
                try:
                    src_meta = src_fut.result()
                except Exception as exc:
                    src_error = str(exc)
                try:
                    tgt_meta = tgt_fut.result()
                except Exception as exc:
                    tgt_error = str(exc)
        elif need_probe_src:
            report(
                22.0,
                "探查表结构",
                f"正在探查来源表({src_schema}.{src_table})结构...",
                0,
                1,
            )
            try:
                src_meta = _probe_side(
                    src_conn["connection_id"],
                    src_schema,
                    src_table,
                    "来源表",
                )
            except Exception as exc:
                src_error = str(exc)
        elif need_probe_tgt:
            report(
                22.0,
                "探查表结构",
                f"正在探查目标表({tgt_schema}.{tgt_table})结构...",
                0,
                1,
            )
            try:
                tgt_meta = _probe_side(
                    tgt_conn["connection_id"],
                    tgt_schema,
                    tgt_table,
                    "目标表",
                )
            except Exception as exc:
                tgt_error = str(exc)

        report(
            72.0,
            "核验数标",
            f"已获取源表({len(src_meta.columns)}列)/目标表({len(tgt_meta.columns)}列)，正在对齐数据标准...",
            0,
            0,
        )

        src_col_map: Dict[str, ColumnMeta] = {}
        for c in src_meta.columns:
            src_col_map[clean_identifier_input(c.column_name).lower()] = c
            src_col_map[c.column_name.lower()] = c

        tgt_col_map: Dict[str, ColumnMeta] = {}
        for c in tgt_meta.columns:
            tgt_col_map[clean_identifier_input(c.column_name).lower()] = c
            tgt_col_map[c.column_name.lower()] = c

        all_saved_std_cols = (
            self.ctx.store.list_cross_columns(t_bind["std_table_key"])
            if load_saved_binding and t_bind.get("std_table_key")
            else []
        )
        std_cols = [
            sc
            for sc in all_saved_std_cols
            if not str(sc.get("std_col_name") or "").startswith("__unpaired_")
        ]
        if not std_cols and (src_meta.columns or tgt_meta.columns):
            seed_cols = src_meta.columns if src_meta.columns else tgt_meta.columns
            std_cols = [
                {
                    "id": -idx,
                    "row_index": idx,
                    "std_table_key": t_bind["std_table_key"],
                    "std_schema_name": t_bind.get("std_schema_name") or src_schema or "public",
                    "std_table_name": t_bind.get("std_table_name") or src_table or tgt_table,
                    "orig_col_name": c.column_name,
                    "orig_col_type": c.data_type,
                    "orig_col_comment": c.column_comment or "",
                    "std_col_name": c.column_name,
                    "std_col_type": c.data_type,
                    "std_col_comment": c.column_comment or "",
                    "bound_source_col": c.column_name,
                    "bound_target_col": c.column_name,
                }
                for idx, c in enumerate(seed_cols, start=1)
            ]

        src_bound_by_col: Dict[str, List[str]] = {}
        tgt_bound_by_col: Dict[str, List[str]] = {}
        enriched_std_cols: List[Dict[str, Any]] = []
        summary = {
            "total": len(std_cols),
            "compliant": 0,
            "target_pending": 0,
            "source_diff": 0,
            "missing": 0,
        }

        for sc in std_cols:
            orig_name = clean_identifier_input(sc["orig_col_name"])
            std_name = clean_identifier_input(sc["std_col_name"]) or orig_name
            std_type = sc["std_col_type"].strip()
            std_comm = sc["std_col_comment"].strip()

            bound_src_name = clean_identifier_input(sc.get("bound_source_col") or "")
            src_col = (
                src_col_map.get(bound_src_name.lower())
                or src_col_map.get(orig_name.lower())
                or src_col_map.get(std_name.lower())
            )
            effective_src_col_name = (
                clean_identifier_input(src_col.column_name)
                if src_col
                else (bound_src_name or orig_name or std_name)
            )

            bound_tgt_name = clean_identifier_input(sc.get("bound_target_col") or "")
            tgt_col = (
                tgt_col_map.get(bound_tgt_name.lower())
                or tgt_col_map.get(std_name.lower())
                or tgt_col_map.get(orig_name.lower())
            )
            effective_tgt_col_name = (
                clean_identifier_input(tgt_col.column_name)
                if tgt_col
                else (bound_tgt_name or std_name or orig_name)
            )

            if src_col:
                src_bound_by_col.setdefault(src_col.column_name.lower(), []).append(std_name)
            if tgt_col:
                tgt_bound_by_col.setdefault(tgt_col.column_name.lower(), []).append(std_name)

            src_status = "UNCONNECTED"
            src_detail = "来源库未连接"
            if src_conn.get("configured"):
                if not src_meta.exists:
                    src_status = "MISSING_TABLE"
                    src_detail = f"来源库不存在表 {src_schema}.{src_table}"
                elif not src_col:
                    src_status = "MISSING_COLUMN"
                    src_detail = f"来源库未找到字段 {effective_src_col_name}"
                else:
                    s_exact, _ = is_cross_type_compatible(src_col.data_type, std_type)
                    if clean_identifier_input(src_col.column_name).lower() == std_name.lower() and s_exact:
                        src_status = "COMPLIANT"
                        src_detail = f"来源列 [{src_col.column_name}] 已符合标准"
                    elif clean_identifier_input(src_col.column_name).lower() == orig_name.lower():
                        src_status = "MATCHED_ORIG"
                        src_detail = f"匹配原字段 [{src_col.column_name} {src_col.data_type}]"
                    else:
                        src_status = "BOUND_DIFF"
                        src_detail = f"绑定来源列 [{src_col.column_name} {src_col.data_type}]"

            tgt_status = "UNCONNECTED"
            tgt_detail = "目标库未连接"
            diffs: List[str] = []
            if tgt_conn.get("configured"):
                if not tgt_meta.exists:
                    tgt_status = "MISSING_TABLE"
                    tgt_detail = f"目标库不存在表 {tgt_schema}.{tgt_table}"
                    summary["missing"] += 1
                elif not tgt_col:
                    tgt_status = "MISSING_COLUMN"
                    tgt_detail = f"目标库未找到字段 {std_name} / {orig_name}"
                    summary["missing"] += 1
                else:
                    name_ok = clean_identifier_input(tgt_col.column_name).lower() == std_name.lower()
                    type_exact, type_compat = is_cross_type_compatible(tgt_col.data_type, std_type)
                    comm_ok = (not std_comm) or ((tgt_col.column_comment or "").strip() == std_comm)

                    if not name_ok:
                        diffs.append(f"字段名待改 ({tgt_col.column_name} -> {std_name})")
                    if not type_exact:
                        if type_compat:
                            diffs.append(f"类型精度差异 ({tgt_col.data_type} vs {std_type})")
                        else:
                            diffs.append(f"类型不符 ({tgt_col.data_type} -> {std_type})")
                    if not comm_ok:
                        diffs.append("注释待更新")

                    if name_ok and type_exact and comm_ok:
                        tgt_status = "COMPLIANT"
                        tgt_detail = "目标库字段名、类型、注释均符合数据标准"
                        summary["compliant"] += 1
                    else:
                        tgt_status = "PENDING_MIGRATION"
                        tgt_detail = "；".join(diffs)
                        summary["target_pending"] += 1

            env_diff_text = ""
            if src_col and tgt_col:
                same_name = clean_identifier_input(src_col.column_name).lower() == clean_identifier_input(tgt_col.column_name).lower()
                same_type = normalize_cross_type(src_col.data_type) == normalize_cross_type(tgt_col.data_type)
                if same_name and same_type:
                    env_diff_text = "两库结构一致"
                else:
                    env_diff_text = f"两库差异: {src_col.column_name}({src_col.data_type}) ↔ {tgt_col.column_name}({tgt_col.data_type})"
                    summary["source_diff"] += 1

            cross_compat = analyze_cross_column_compatibility(
                src_col.data_type if src_col else "",
                tgt_col.data_type if tgt_col else "",
                source_db_type=str(src_conn.get("db_type") or ""),
                target_db_type=str(tgt_conn.get("db_type") or ""),
            )

            enriched_std_cols.append(
                {
                    **sc,
                    "effective_source_col": effective_src_col_name,
                    "effective_target_col": effective_tgt_col_name,
                    "source_col_meta": (
                        {
                            "ordinal_position": src_col.ordinal_position,
                            "column_name": src_col.column_name,
                            "data_type": src_col.data_type,
                            "column_comment": src_col.column_comment,
                        }
                        if src_col
                        else None
                    ),
                    "target_col_meta": (
                        {
                            "ordinal_position": tgt_col.ordinal_position,
                            "column_name": tgt_col.column_name,
                            "data_type": tgt_col.data_type,
                            "column_comment": tgt_col.column_comment,
                        }
                        if tgt_col
                        else None
                    ),
                    "source_status": src_status,
                    "source_detail": src_detail,
                    "target_status": tgt_status,
                    "target_detail": tgt_detail,
                    "env_diff_text": env_diff_text,
                    "cross_compat": cross_compat,
                }
            )

        src_dist_set = {k.lower() for k in (src_meta.distribute_keys + src_meta.partition_keys + src_meta.primary_keys)}
        tgt_dist_set = {k.lower() for k in (tgt_meta.distribute_keys + tgt_meta.partition_keys + tgt_meta.primary_keys)}

        source_columns_view = [
            {
                "ordinal_position": c.ordinal_position,
                "column_name": clean_identifier_input(c.column_name) or c.column_name,
                "raw_column_name": c.column_name,
                "data_type": c.data_type,
                "column_comment": c.column_comment,
                "lineage_source": c.lineage_source or "",
                "is_key": clean_identifier_input(c.column_name).lower() in src_dist_set,
                "bound_std_cols": src_bound_by_col.get(c.column_name.lower(), []),
            }
            for c in src_meta.columns
        ]

        tgt_status_by_col: Dict[str, Tuple[str, str]] = {}
        for esc in enriched_std_cols:
            t_meta = esc.get("target_col_meta")
            if t_meta:
                tgt_status_by_col[t_meta["column_name"].lower()] = (
                    esc["target_status"],
                    esc["target_detail"],
                )

        target_columns_view = [
            {
                "ordinal_position": c.ordinal_position,
                "column_name": clean_identifier_input(c.column_name) or c.column_name,
                "raw_column_name": c.column_name,
                "data_type": c.data_type,
                "column_comment": c.column_comment,
                "lineage_source": c.lineage_source or "",
                "is_key": clean_identifier_input(c.column_name).lower() in tgt_dist_set,
                "bound_std_cols": tgt_bound_by_col.get(c.column_name.lower(), []),
                "compliance_status": tgt_status_by_col.get(c.column_name.lower(), ("UNBOUND", "未绑定数标"))[0],
                "compliance_detail": tgt_status_by_col.get(c.column_name.lower(), ("UNBOUND", "未在当前数标清单中声明"))[1],
            }
            for c in tgt_meta.columns
        ]

        report(
            92.0,
            "构建比对视图",
            "正在生成五虚拟列对照矩阵...",
            0,
            0,
        )
        dual_rows, bookmark_summary = self._build_dual_bookmark_rows(
            src_conn=src_conn,
            tgt_conn=tgt_conn,
            src_meta=src_meta,
            tgt_meta=tgt_meta,
            src_schema=src_meta.schema_name if src_meta.exists else src_schema,
            src_table=src_meta.table_name if src_meta.exists else src_table,
            tgt_schema=tgt_meta.schema_name if tgt_meta.exists else tgt_schema,
            tgt_table=tgt_meta.table_name if tgt_meta.exists else tgt_table,
            saved_cols=all_saved_std_cols,
            format_profile=format_profile,
            source_format_profile=eff_src_fmt,
            target_format_profile=eff_tgt_fmt,
        )

        return {
            "table_binding": {
                **t_bind,
                "source_schema": src_meta.schema_name if src_meta.exists else src_schema,
                "source_table": src_meta.table_name if src_meta.exists else src_table,
                "target_schema": tgt_meta.schema_name if tgt_meta.exists else tgt_schema,
                "target_table": tgt_meta.table_name if tgt_meta.exists else tgt_table,
            },
            "summary": summary,
            "bookmark_summary": bookmark_summary,
            "dual_rows": dual_rows,
            "format_profile": format_profile or "AUTO",
            "source_format_profile": eff_src_fmt,
            "target_format_profile": eff_tgt_fmt,
            "source_panel": {
                "configured": bool(src_conn.get("configured")),
                "connection_id": src_conn.get("connection_id", ""),
                "connection_name": src_conn.get("name", ""),
                "db_type": src_conn.get("db_type", "DWS"),
                "standard_format": eff_src_fmt,
                "schema_name": src_meta.schema_name if src_meta.exists else src_schema,
                "table_name": src_meta.table_name if src_meta.exists else src_table,
                "exists": src_meta.exists,
                "object_type": src_meta.object_type or "TABLE",
                "view_definition": src_meta.view_definition or "",
                "penetrated_base_tables": list(src_meta.penetrated_base_tables or []),
                "table_comment": src_meta.table_comment,
                "distribute_keys": src_meta.distribute_keys,
                "partition_keys": src_meta.partition_keys,
                "error": src_error,
                "columns": source_columns_view,
            },
            "standard_panel": {
                "std_table_key": t_bind["std_table_key"],
                "std_schema_name": t_bind["std_schema_name"],
                "std_table_name": t_bind["std_table_name"],
                "columns": enriched_std_cols,
            },
            "target_panel": {
                "configured": bool(tgt_conn.get("configured")),
                "connection_id": tgt_conn.get("connection_id", ""),
                "connection_name": tgt_conn.get("name", ""),
                "db_type": tgt_conn.get("db_type", "DWS"),
                "standard_format": eff_tgt_fmt,
                "schema_name": tgt_meta.schema_name if tgt_meta.exists else tgt_schema,
                "table_name": tgt_meta.table_name if tgt_meta.exists else tgt_table,
                "exists": tgt_meta.exists,
                "object_type": tgt_meta.object_type or "TABLE",
                "view_definition": tgt_meta.view_definition or "",
                "penetrated_base_tables": list(tgt_meta.penetrated_base_tables or []),
                "table_comment": tgt_meta.table_comment,
                "distribute_keys": tgt_meta.distribute_keys,
                "partition_keys": tgt_meta.partition_keys,
                "error": tgt_error,
                "columns": target_columns_view,
            },
        }

    def _build_dual_bookmark_rows(
        self,
        *,
        src_conn: Dict[str, Any],
        tgt_conn: Dict[str, Any],
        src_meta: TableMeta,
        tgt_meta: TableMeta,
        src_schema: str,
        src_table: str,
        tgt_schema: str,
        tgt_table: str,
        saved_cols: List[Dict[str, Any]],
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """构建双列 + 内侧中缝拟物书签行模型（自动继承第 2 步字段级数标绑定）."""
        src_cid = str(src_conn.get("connection_id") or "")
        tgt_cid = str(tgt_conn.get("connection_id") or "")
        src_db_type = str(src_conn.get("db_type") or "DWS")
        tgt_db_type = str(tgt_conn.get("db_type") or "DWS")
        eff_src_fmt = self._resolve_side_format_profile(
            src_conn, source_format_profile, format_profile
        )
        eff_tgt_fmt = self._resolve_side_format_profile(
            tgt_conn, target_format_profile, format_profile
        )

        src_field_binds = (
            self.ctx.standard_catalog.get_table_field_bindings(src_cid, src_schema, src_table)
            if src_cid and src_table
            else {}
        )
        tgt_field_binds = (
            self.ctx.standard_catalog.get_table_field_bindings(tgt_cid, tgt_schema, tgt_table)
            if tgt_cid and tgt_table
            else {}
        )

        src_col_map: Dict[str, ColumnMeta] = {}
        for c in src_meta.columns:
            ck = (clean_identifier_input(c.column_name) or c.column_name).lower()
            src_col_map[ck] = c
            src_col_map[c.column_name.lower()] = c

        tgt_col_map: Dict[str, ColumnMeta] = {}
        for c in tgt_meta.columns:
            ck = (clean_identifier_input(c.column_name) or c.column_name).lower()
            tgt_col_map[ck] = c
            tgt_col_map[c.column_name.lower()] = c

        def _col_dict(c: Optional[ColumnMeta]) -> Optional[Dict[str, Any]]:
            if not c:
                return None
            return {
                "ordinal_position": c.ordinal_position,
                "column_name": clean_identifier_input(c.column_name) or c.column_name,
                "raw_column_name": c.column_name,
                "data_type": c.data_type,
                "column_comment": c.column_comment or "",
                "lineage_source": c.lineage_source or "",
            }

        raw_pairs: List[Dict[str, Any]] = []
        used_src: set[str] = set()
        used_tgt: set[str] = set()

        # 1. 先消费显式保存的配对/拆解记录
        for sc in saved_cols:
            sc_name = str(sc.get("std_col_name") or "")
            b_src = clean_identifier_input(sc.get("bound_source_col") or "")
            b_tgt = clean_identifier_input(sc.get("bound_target_col") or "")
            if not sc_name.startswith("__unpaired_"):
                if not b_src:
                    b_src = clean_identifier_input(sc.get("orig_col_name") or sc_name)
                if not b_tgt:
                    b_tgt = clean_identifier_input(sc_name or sc.get("orig_col_name") or "")

            src_obj = src_col_map.get(b_src.lower()) if b_src else None
            tgt_obj = tgt_col_map.get(b_tgt.lower()) if b_tgt else None

            eff_src_name = (
                (clean_identifier_input(src_obj.column_name) or src_obj.column_name)
                if src_obj
                else b_src
            )
            eff_tgt_name = (
                (clean_identifier_input(tgt_obj.column_name) or tgt_obj.column_name)
                if tgt_obj
                else b_tgt
            )
            if not eff_src_name and not eff_tgt_name:
                continue

            # 若该列名已在物理库中存在则用真实列对象，否则展示占位列信息（如 Excel 离线配置尚未建表）
            if eff_src_name and eff_src_name.lower() in used_src:
                eff_src_name = ""
                src_obj = None
            if eff_tgt_name and eff_tgt_name.lower() in used_tgt:
                eff_tgt_name = ""
                tgt_obj = None
            if not eff_src_name and not eff_tgt_name:
                continue

            if eff_src_name:
                used_src.add(eff_src_name.lower())
            if eff_tgt_name:
                used_tgt.add(eff_tgt_name.lower())

            raw_pairs.append(
                {
                    "src_name": eff_src_name,
                    "src_obj": src_obj,
                    "tgt_name": eff_tgt_name,
                    "tgt_obj": tgt_obj,
                    "saved_src_std": clean_excel_cell_str(
                        sc.get("source_std_code"), treat_placeholder_empty=True
                    ),
                    "saved_tgt_std": clean_excel_cell_str(
                        sc.get("target_std_code"), treat_placeholder_empty=True
                    ),
                    "fallback_std_name": "" if sc_name.startswith("__unpaired_") else sc_name,
                    "fallback_std_type": str(sc.get("std_col_type") or "").strip(),
                    "fallback_std_comment": str(sc.get("std_col_comment") or "").strip(),
                }
            )

        # 2. 对尚未被显式声明覆盖的库内新增字段：按相同非空 std_code 或同名自动配对
        leftover_src = [
            c
            for c in src_meta.columns
            if (clean_identifier_input(c.column_name) or c.column_name).lower() not in used_src
        ]
        leftover_tgt = [
            c
            for c in tgt_meta.columns
            if (clean_identifier_input(c.column_name) or c.column_name).lower() not in used_tgt
        ]

        # 按 1:1 非空 std_code 索引目标剩余列
        tgt_by_std: Dict[str, List[ColumnMeta]] = {}
        tgt_by_name: Dict[str, ColumnMeta] = {}
        for tc in leftover_tgt:
            tc_key = (clean_identifier_input(tc.column_name) or tc.column_name).lower()
            tgt_by_name[tc_key] = tc
            t_code = (tgt_field_binds.get(tc_key, {}).get("std_code") or "").strip()
            if t_code:
                tgt_by_std.setdefault(t_code, []).append(tc)

        src_std_counts: Dict[str, int] = {}
        for sc_col in leftover_src:
            sc_key = (clean_identifier_input(sc_col.column_name) or sc_col.column_name).lower()
            s_code = (src_field_binds.get(sc_key, {}).get("std_code") or "").strip()
            if s_code:
                src_std_counts[s_code] = src_std_counts.get(s_code, 0) + 1

        paired_tgt_left: set[str] = set()
        for sc_col in leftover_src:
            sc_name = clean_identifier_input(sc_col.column_name) or sc_col.column_name
            sc_key = sc_name.lower()
            s_code = (src_field_binds.get(sc_key, {}).get("std_code") or "").strip()
            matched_tgt: Optional[ColumnMeta] = None

            if (
                s_code
                and src_std_counts.get(s_code) == 1
                and len(tgt_by_std.get(s_code, [])) == 1
            ):
                cand_t = tgt_by_std[s_code][0]
                cand_t_key = (clean_identifier_input(cand_t.column_name) or cand_t.column_name).lower()
                if cand_t_key not in paired_tgt_left:
                    matched_tgt = cand_t

            if matched_tgt is None and sc_key in tgt_by_name:
                cand_t = tgt_by_name[sc_key]
                cand_t_key = (clean_identifier_input(cand_t.column_name) or cand_t.column_name).lower()
                if cand_t_key not in paired_tgt_left:
                    matched_tgt = cand_t

            if matched_tgt is not None:
                m_name = clean_identifier_input(matched_tgt.column_name) or matched_tgt.column_name
                paired_tgt_left.add(m_name.lower())
                raw_pairs.append(
                    {
                        "src_name": sc_name,
                        "src_obj": sc_col,
                        "tgt_name": m_name,
                        "tgt_obj": matched_tgt,
                        "saved_src_std": "",
                        "saved_tgt_std": "",
                        "fallback_std_name": "",
                        "fallback_std_type": "",
                        "fallback_std_comment": "",
                    }
                )
            else:
                raw_pairs.append(
                    {
                        "src_name": sc_name,
                        "src_obj": sc_col,
                        "tgt_name": "",
                        "tgt_obj": None,
                        "saved_src_std": "",
                        "saved_tgt_std": "",
                        "fallback_std_name": "",
                        "fallback_std_type": "",
                        "fallback_std_comment": "",
                    }
                )

        for tc in leftover_tgt:
            tc_name = clean_identifier_input(tc.column_name) or tc.column_name
            if tc_name.lower() in paired_tgt_left:
                continue
            raw_pairs.append(
                {
                    "src_name": "",
                    "src_obj": None,
                    "tgt_name": tc_name,
                    "tgt_obj": tc,
                    "saved_src_std": "",
                    "saved_tgt_std": "",
                    "fallback_std_name": "",
                    "fallback_std_type": "",
                    "fallback_std_comment": "",
                }
            )

        src_has_step2 = bool(src_cid and src_table and (src_meta.exists or src_field_binds))
        tgt_has_step2 = bool(tgt_cid and tgt_table and (tgt_meta.exists or tgt_field_binds))

        # 收集所有涉及的标准编号批量查表
        needed_codes: List[str] = []
        for rp in raw_pairs:
            s_n = rp["src_name"].lower()
            t_n = rp["tgt_name"].lower()
            if s_n and s_n in src_field_binds:
                s_code = (src_field_binds[s_n].get("std_code") or "").strip()
            else:
                s_code = "" if src_has_step2 else rp["saved_src_std"].strip()
            if t_n and t_n in tgt_field_binds:
                t_code = (tgt_field_binds[t_n].get("std_code") or "").strip()
            else:
                t_code = "" if tgt_has_step2 else rp["saved_tgt_std"].strip()
            rp["eff_src_code"] = s_code
            rp["eff_tgt_code"] = t_code
            if s_code:
                needed_codes.append(s_code)
            if t_code:
                needed_codes.append(t_code)

        std_map = self.ctx.standard_catalog.get_items_by_codes(needed_codes)

        def _std_brief(code: str, db_type: str, side_fmt: str) -> Optional[Dict[str, Any]]:
            if not code:
                return None
            it = std_map.get(code)
            if not it:
                return {
                    "std_code": code,
                    "cn_name": "字典缺失",
                    "en_abbr": "",
                    "resolved_type": "",
                    "domain_name": "",
                    "missing": True,
                }
            return {
                "std_code": it["std_code"],
                "cn_name": it["cn_name"],
                "en_abbr": it["en_abbr"],
                "resolved_type": self.ctx.standard_catalog.resolve_physical_type(
                    it, db_type=db_type, format_profile=side_fmt
                ),
                "domain_name": it.get("domain_name", ""),
                "missing": False,
            }

        dual_rows: List[Dict[str, Any]] = []
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

        for idx, rp in enumerate(raw_pairs, start=1):
            src_name = rp["src_name"]
            tgt_name = rp["tgt_name"]
            s_code = rp["eff_src_code"]
            t_code = rp["eff_tgt_code"]

            src_col_info = _col_dict(rp["src_obj"])
            if not src_col_info and src_name:
                src_col_info = {
                    "ordinal_position": idx,
                    "column_name": src_name,
                    "raw_column_name": src_name,
                    "data_type": rp["fallback_std_type"] or "",
                    "column_comment": rp["fallback_std_comment"] or "",
                    "lineage_source": "",
                    "virtual": True,
                }

            tgt_col_info = _col_dict(rp["tgt_obj"])
            if not tgt_col_info and tgt_name:
                tgt_col_info = {
                    "ordinal_position": idx,
                    "column_name": tgt_name,
                    "raw_column_name": tgt_name,
                    "data_type": "",
                    "column_comment": "",
                    "lineage_source": "",
                    "virtual": True,
                }

            paired = bool(src_col_info and tgt_col_info)
            if paired and s_code and s_code == t_code:
                bk_state = "UNIFIED"
                bk_summary["unified"] += 1
            elif s_code and t_code and s_code != t_code:
                bk_state = "SPLIT_DIFF"
                bk_summary["split_diff"] += 1
            elif s_code or t_code:
                bk_state = "SPLIT_PARTIAL"
                bk_summary["split_partial"] += 1
            else:
                bk_state = "SPLIT_UNBOUND"
                bk_summary["split_unbound"] += 1

            if paired:
                bk_summary["paired_rows"] += 1
            bk_summary["total_rows"] += 1

            src_std_brief = _std_brief(s_code, src_db_type, eff_src_fmt)
            tgt_std_brief = _std_brief(t_code, tgt_db_type, eff_tgt_fmt)

            if paired and src_col_info and tgt_col_info:
                cross_compat = analyze_cross_column_compatibility(
                    str(src_col_info.get("data_type") or ""),
                    str(tgt_col_info.get("data_type") or ""),
                    source_db_type=src_db_type,
                    target_db_type=tgt_db_type,
                )
            else:
                cross_compat = analyze_cross_column_compatibility("", "")

            if cross_compat.get("applicable"):
                if cross_compat["capacity_status"] == "INSUFFICIENT":
                    bk_summary["capacity_insufficient"] += 1
                elif cross_compat["capacity_status"] == "RISK":
                    bk_summary["capacity_risk"] += 1
                if cross_compat.get("has_type_conversion"):
                    bk_summary["type_conversion"] += 1

            # 判定目标库字段相对绑定标准的物理合规度（供淡绿书签附「待贯标」轻角标）
            target_compliance = None
            target_needs_migration = False
            if tgt_col_info and t_code:
                t_item = std_map.get(t_code)
                target_compliance = self.ctx.standard_catalog.evaluate_column_compliance(
                    col_name=tgt_col_info["column_name"],
                    col_type=tgt_col_info["data_type"],
                    col_comment=tgt_col_info["column_comment"],
                    std_item=t_item,
                    std_code=t_code,
                    db_type=tgt_db_type,
                    format_profile=eff_tgt_fmt,
                )
                if target_compliance["status"] in ("NON_COMPLIANT", "COMMENT_ONLY", "STD_MISSING"):
                    target_needs_migration = True
                    bk_summary["target_needs_migration"] += 1

            dual_rows.append(
                {
                    "row_id": f"row_{idx}_{src_name or 'none'}_{tgt_name or 'none'}",
                    "paired": paired,
                    "bookmark_state": bk_state,
                    "source_col": src_col_info,
                    "target_col": tgt_col_info,
                    "source_std_code": s_code,
                    "target_std_code": t_code,
                    "source_std": src_std_brief,
                    "target_std": tgt_std_brief,
                    "target_compliance": target_compliance,
                    "target_needs_migration": target_needs_migration,
                    "cross_compat": cross_compat,
                    "fallback_std_name": rp["fallback_std_name"],
                    "fallback_std_type": rp["fallback_std_type"],
                    "fallback_std_comment": rp["fallback_std_comment"],
                }
            )

        return dual_rows, bk_summary

    def save_pairings(
        self,
        *,
        std_table_key: str,
        source_schema: str,
        source_table: str,
        target_schema: str,
        target_table: str,
        pairings: List[Dict[str, Any]],
        format_profile: str = "AUTO",
        source_format_profile: str = "AUTO",
        target_format_profile: str = "AUTO",
        force_refresh: bool = False,
        sync_to_field_bindings: bool = True,
        return_overview: bool = True,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        """保存双列拖拽调整后的字段映射关系与两侧数标，并可选双向回写第 2 步数据源绑定."""
        report = progress_cb or noop_progress
        report(
            18.0,
            "校验映射规则",
            f"正在整理 {len(pairings)} 条字段映射与数据标准...",
            0,
            len(pairings),
        )
        src_cid = str(self.ctx.store.get_meta("cross_source_connection_id", "") or "")
        tgt_cid = str(self.ctx.store.get_meta("cross_target_connection_id", "") or "")
        src_prof = self.ctx.connection_hub.get_profile(src_cid) if src_cid else None
        tgt_prof = self.ctx.connection_hub.get_profile(tgt_cid) if tgt_cid else None
        src_def_sch = src_prof.effective_default_schema() if src_prof else "public"
        tgt_def_sch = tgt_prof.effective_default_schema() if tgt_prof else "public"

        s_sch = (source_schema or "").strip()
        s_tbl = (source_table or "").strip()
        if "." in s_tbl:
            p0, p1 = s_tbl.split(".", 1)
            if not s_sch:
                s_sch = p0.strip()
            s_tbl = p1.strip()
        s_sch = s_sch or src_def_sch

        t_sch = (target_schema or "").strip()
        t_tbl = (target_table or "").strip()
        if "." in t_tbl:
            p0, p1 = t_tbl.split(".", 1)
            if not t_sch:
                t_sch = p0.strip()
            t_tbl = p1.strip()
        t_sch = t_sch or tgt_def_sch

        eff_key = clean_identifier_input(std_table_key or "").lower()
        if not eff_key:
            eff_key = clean_identifier_input(t_tbl or s_tbl).lower()
        if not eff_key:
            raise ValueError("请先在来源库或目标库中选择要映射的表或视图")

        self.ctx.store.upsert_cross_table_binding(
            eff_key,
            std_schema_name=t_sch or s_sch,
            source_schema=s_sch,
            source_table=s_tbl or eff_key,
            target_schema=t_sch,
            target_table=t_tbl or eff_key,
        )

        tgt_db_type = tgt_prof.db_type if tgt_prof else "DWS"
        eff_tgt_fmt = self._resolve_side_format_profile(
            {"standard_format": getattr(tgt_prof, "standard_format", "AUTO")} if tgt_prof else {},
            target_format_profile,
            format_profile,
        )
        src_sync_batch: List[Dict[str, Any]] = []
        tgt_sync_batch: List[Dict[str, Any]] = []

        # 收集标准字典信息用于填充标准名称与目标物理类型
        codes_to_fetch = []
        for p in pairings:
            sc_clean = clean_excel_cell_str(p.get("source_std_code"), treat_placeholder_empty=True)
            tc_clean = clean_excel_cell_str(p.get("target_std_code"), treat_placeholder_empty=True)
            if sc_clean:
                codes_to_fetch.append(sc_clean)
            if tc_clean:
                codes_to_fetch.append(tc_clean)
        std_map = self.ctx.standard_catalog.get_items_by_codes(codes_to_fetch)

        now = self.ctx.store._now_str()
        col_rows_to_insert: List[Tuple[Any, ...]] = []
        for idx, p in enumerate(pairings, start=1):
            s_col = clean_identifier_input(clean_excel_cell_str(p.get("source_col"), treat_placeholder_empty=True))
            t_col = clean_identifier_input(clean_excel_cell_str(p.get("target_col"), treat_placeholder_empty=True))
            s_code = clean_excel_cell_str(p.get("source_std_code"), treat_placeholder_empty=True)
            t_code = clean_excel_cell_str(p.get("target_std_code"), treat_placeholder_empty=True)
            if not s_col and not t_col:
                continue

            if s_col and t_col:
                rep_item = std_map.get(t_code) or std_map.get(s_code)
                std_col_name = (
                    (rep_item.get("en_abbr") if rep_item else "")
                    or t_col
                    or s_col
                )
                std_col_type = (
                    self.ctx.standard_catalog.resolve_physical_type(
                        rep_item, db_type=tgt_db_type, format_profile=eff_tgt_fmt
                    )
                    if rep_item
                    else str(p.get("std_col_type") or "")
                )
                std_col_comm = (
                    rep_item.get("cn_name", "")
                    if rep_item
                    else str(p.get("std_col_comment") or "")
                )
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

            col_rows_to_insert.append(
                (
                    eff_key,
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

            if sync_to_field_bindings:
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
            42.0,
            "保存映射关系",
            f"正在写入 [{eff_key}] {len(col_rows_to_insert)} 行字段映射...",
            len(col_rows_to_insert),
            max(1, len(col_rows_to_insert)),
        )
        with self.ctx.store.module_db() as conn:
            conn.execute("DELETE FROM cross_column_bindings WHERE std_table_key = ?", (eff_key,))
            if col_rows_to_insert:
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
                    col_rows_to_insert,
                )

        if sync_to_field_bindings:
            total_sync = len(src_sync_batch) + len(tgt_sync_batch)
            if total_sync > 0:
                report(
                    60.0,
                    "双向回写数标",
                    f"正在同步 {total_sync} 条字段数标绑定至单库档案...",
                    0,
                    total_sync,
                )
            if src_sync_batch:
                self.ctx.standard_catalog.upsert_field_bindings(
                    src_cid, src_sync_batch, bind_source="CROSS_SYNC"
                )
            if tgt_sync_batch:
                self.ctx.standard_catalog.upsert_field_bindings(
                    tgt_cid, tgt_sync_batch, bind_source="CROSS_SYNC"
                )

        self.ctx.store.add_log(
            "INFO",
            f"跨环境双列映射 [{eff_key}]",
            f"已保存 {len(col_rows_to_insert)} 行字段映射与书签绑定（来源 {s_sch}.{s_tbl or eff_key} ↔ 目标 {t_sch}.{t_tbl or eff_key}）",
        )
        if not return_overview:
            return {
                "ok": True,
                "std_table_key": eff_key,
                "saved_count": len(col_rows_to_insert),
            }
        report(
            74.0,
            "刷新比对视图",
            f"正在重新校验 [{eff_key}] 落标状态...",
            0,
            0,
        )
        state = self.get_overview_state(
            selected_table_key=eff_key,
            force_refresh=force_refresh,
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
            progress_cb=report,
        )
        state["message"] = "已保存双列映射与书签绑定"
        return state

    def save_bindings(
        self,
        *,
        std_table_key: str,
        source_schema: str,
        source_table: str,
        target_schema: str,
        target_table: str,
        column_bindings: Optional[List[Dict[str, Any]]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        s_sch = (source_schema or "").strip()
        s_tbl = (source_table or "").strip()
        if "." in s_tbl:
            p0, p1 = s_tbl.split(".", 1)
            if not s_sch:
                s_sch = p0
            s_tbl = p1

        t_sch = (target_schema or "").strip()
        t_tbl = (target_table or "").strip()
        if "." in t_tbl:
            p0, p1 = t_tbl.split(".", 1)
            if not t_sch:
                t_sch = p0
            t_tbl = p1

        eff_key = (std_table_key or "").strip()
        if "." in eff_key:
            eff_key = eff_key.split(".", 1)[1].strip()
        if not eff_key:
            eff_key = s_tbl or t_tbl
        if not eff_key:
            raise ValueError("请先在来源库或目标库中选择/输入要读取的表名或视图名")

        self.ctx.store.upsert_cross_table_binding(
            eff_key,
            source_schema=s_sch,
            source_table=s_tbl or eff_key,
            target_schema=t_sch,
            target_table=t_tbl or eff_key,
        )
        if column_bindings:
            self.ctx.store.upsert_cross_table_column_bindings(eff_key, column_bindings)
            src_cid = str(self.ctx.store.get_meta("cross_source_connection_id", "") or "")
            tgt_cid = str(self.ctx.store.get_meta("cross_target_connection_id", "") or "")
            src_prof = self.ctx.connection_hub.get_profile(src_cid) if src_cid else None
            tgt_prof = self.ctx.connection_hub.get_profile(tgt_cid) if tgt_cid else None
            eff_s_sch = s_sch or (src_prof.effective_default_schema() if src_prof else "public")
            eff_t_sch = t_sch or (tgt_prof.effective_default_schema() if tgt_prof else "public")
            eff_s_tbl = s_tbl or eff_key
            eff_t_tbl = t_tbl or eff_key
            src_sync: List[Dict[str, Any]] = []
            tgt_sync: List[Dict[str, Any]] = []
            for cb in column_bindings:
                b_src = clean_identifier_input(cb.get("bound_source_col") or "")
                b_tgt = clean_identifier_input(cb.get("bound_target_col") or "")
                if src_cid and b_src and "source_std_code" in cb:
                    src_sync.append(
                        {
                            "schema_name": eff_s_sch,
                            "table_name": eff_s_tbl,
                            "column_name": b_src,
                            "std_code": clean_excel_cell_str(
                                cb.get("source_std_code"), treat_placeholder_empty=True
                            ),
                        }
                    )
                if tgt_cid and b_tgt and "target_std_code" in cb:
                    tgt_sync.append(
                        {
                            "schema_name": eff_t_sch,
                            "table_name": eff_t_tbl,
                            "column_name": b_tgt,
                            "std_code": clean_excel_cell_str(
                                cb.get("target_std_code"), treat_placeholder_empty=True
                            ),
                        }
                    )
            if src_sync:
                self.ctx.standard_catalog.upsert_field_bindings(
                    src_cid, src_sync, bind_source="CROSS_SYNC"
                )
            if tgt_sync:
                self.ctx.standard_catalog.upsert_field_bindings(
                    tgt_cid, tgt_sync, bind_source="CROSS_SYNC"
                )

        self.ctx.store.add_log(
            "INFO",
            f"三栏比对 [{eff_key}]",
            f"已持久保存 [{eff_key}] 的三方绑定关系：来源({s_sch}.{s_tbl or eff_key}) ↔ 数标 ↔ 目标({t_sch}.{t_tbl or eff_key})",
        )
        return self.get_overview_state(selected_table_key=eff_key, force_refresh=force_refresh)
