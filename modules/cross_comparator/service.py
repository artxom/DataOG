"""16:9 跨环境三栏数标比对中心服务 (CrossComparatorService).

严格遵循插件隔离规范：
1. 仅依赖 `core.platform` 公共底座，严禁导入任何其他 `modules.*` 私有模块；
2. 全面接入 `ConnectionHub` 与 `MetadataHub`，左栏（来源库）与右栏（目标库）支持任意内置原生驱动与自定义 JDBC 驱动数据源；
3. 推送待贯标改造项至执行台一律通过 `PlatformStateStore` 状态存储契约完成。
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.platform.context import PlatformContext
from core.platform.dialects import (
    clean_identifier_input,
    is_cross_type_compatible,
    normalize_cross_type,
)
from core.platform.excel_io import (
    ResolvedWorkbook,
    clean_excel_cell_str,
    parse_excel_checklist,
    validate_xlsx_bytes,
)
from core.platform.import_progress import ProgressCallback, noop_progress
from core.platform.models import ColumnMeta, TableMeta


class CrossComparatorService:
    """跨环境三栏数标比对中心核心服务."""

    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def _meta_key_for_role(self, role: str) -> str:
        r = (role or "").strip().lower()
        if r not in ("source", "target"):
            raise ValueError("环境角色必须为 source (来源库) 或 target (目标库)")
        return f"cross_{r}_connection_id"

    def get_role_connection_info(self, role: str) -> Dict[str, Any]:
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
                "user": "",
                "standard_format": "AUTO",
                "schemas": [],
                "tables": [],
            }

        cat = self.ctx.metadata_hub.get_catalog_tree(prof.connection_id, auto_refresh=False)
        pub = prof.to_public_dict()
        return {
            **pub,
            "role": role,
            "configured": True,
            "schema_name": prof.default_schema,
            "schemas": [s["schema_name"] if isinstance(s, dict) else str(s) for s in cat["schemas"]],
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
    ) -> Dict[str, Any]:
        meta_key = self._meta_key_for_role(role)
        if not connection_id:
            self.ctx.store.save_meta(meta_key, "")
            return self.get_overview_state(selected_table_key=active_table_key)

        prof = self.ctx.connection_hub.require_profile(connection_id)
        self.ctx.store.save_meta(meta_key, prof.connection_id)
        warning = ""
        if auto_refresh:
            try:
                self.ctx.metadata_hub.refresh_catalog(prof.connection_id)
            except Exception as exc:
                warning = f"已绑定连接 [{prof.name}]，但自动刷新目录缓存未完成: {exc}"

        state = self.get_overview_state(selected_table_key=active_table_key)
        if warning:
            state["warning"] = warning
        state["message"] = f"已将 [{prof.name}] 绑定为{'来源库 (左栏)' if role == 'source' else '目标库 (右栏)'}"
        return state

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
    ) -> Dict[str, Any]:
        tables = self.ctx.store.list_cross_tables()
        if not tables and self.ctx.store.list_items():
            fn = self.ctx.store.get_meta("current_excel_name", "执行台同步清单.xlsx")
            tables = self.ctx.store.replace_cross_standards(
                self.ctx.store.list_items(), fn, preserve_bindings=True
            )

        active_key = (selected_table_key or "").strip().lower()
        if not active_key and tables and not (source_table or target_table):
            active_key = tables[0]["std_table_key"]

        src_conn = self.get_role_connection_info("source")
        tgt_conn = self.get_role_connection_info("target")
        eff_src_fmt = self._resolve_side_format_profile(
            src_conn, source_format_profile, format_profile
        )
        eff_tgt_fmt = self._resolve_side_format_profile(
            tgt_conn, target_format_profile, format_profile
        )
        eff_src_fmt = self._resolve_side_format_profile(
            src_conn, source_format_profile, format_profile
        )
        eff_tgt_fmt = self._resolve_side_format_profile(
            tgt_conn, target_format_profile, format_profile
        )

        comp = None
        if active_key or source_table or target_table:
            comp = self.compare_single_table(
                active_key or (source_table or target_table or "").strip().lower(),
                override_source_schema=source_schema,
                override_source_table=source_table,
                override_target_schema=target_schema,
                override_target_table=target_table,
                force_refresh=force_refresh,
                format_profile=format_profile,
                source_format_profile=eff_src_fmt,
                target_format_profile=eff_tgt_fmt,
                persist_missing_binding=bool(active_key),
            )
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
                "column_count": len(self.ctx.store.list_cross_columns()),
            },
            "tables": tables,
            "active_table_key": active_key,
            "active_comparison": comp,
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
    ) -> Dict[str, Any]:
        t_bind = self.ctx.store.get_cross_table_binding(std_table_key)
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

        src_conn = self.get_role_connection_info("source")
        tgt_conn = self.get_role_connection_info("target")
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

        if src_conn.get("configured") and src_conn.get("connection_id"):
            try:
                src_meta = self.ctx.metadata_hub.get_table_metadata(
                    src_conn["connection_id"],
                    src_schema,
                    src_table,
                    force_refresh=force_refresh,
                    penetrate_views=True,
                )
            except Exception as exc:
                src_error = str(exc)

        if tgt_conn.get("configured") and tgt_conn.get("connection_id"):
            try:
                tgt_meta = self.ctx.metadata_hub.get_table_metadata(
                    tgt_conn["connection_id"],
                    tgt_schema,
                    tgt_table,
                    force_refresh=force_refresh,
                    penetrate_views=True,
                )
            except Exception as exc:
                tgt_error = str(exc)

        src_col_map: Dict[str, ColumnMeta] = {}
        for c in src_meta.columns:
            src_col_map[clean_identifier_input(c.column_name).lower()] = c
            src_col_map[c.column_name.lower()] = c

        tgt_col_map: Dict[str, ColumnMeta] = {}
        for c in tgt_meta.columns:
            tgt_col_map[clean_identifier_input(c.column_name).lower()] = c
            tgt_col_map[c.column_name.lower()] = c

        all_saved_std_cols = self.ctx.store.list_cross_columns(t_bind["std_table_key"])
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

        # 收集所有涉及的标准编号批量查表
        needed_codes: List[str] = []
        for rp in raw_pairs:
            s_n = rp["src_name"].lower()
            t_n = rp["tgt_name"].lower()
            s_code = (src_field_binds.get(s_n, {}).get("std_code") or rp["saved_src_std"]).strip()
            t_code = (tgt_field_binds.get(t_n, {}).get("std_code") or rp["saved_tgt_std"]).strip()
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
    ) -> Dict[str, Any]:
        """保存双列拖拽调整后的字段映射关系与两侧数标，并可选双向回写第 2 步数据源绑定."""
        s_sch = (source_schema or "").strip() or "public"
        s_tbl = (source_table or "").strip()
        if "." in s_tbl:
            s_sch, s_tbl = s_tbl.split(".", 1)

        t_sch = (target_schema or "").strip() or "public"
        t_tbl = (target_table or "").strip()
        if "." in t_tbl:
            t_sch, t_tbl = t_tbl.split(".", 1)

        eff_key = clean_identifier_input(std_table_key or "").lower()
        if not eff_key:
            eff_key = clean_identifier_input(s_tbl or t_tbl).lower()
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

        src_cid = str(self.ctx.store.get_meta("cross_source_connection_id", "") or "")
        tgt_cid = str(self.ctx.store.get_meta("cross_target_connection_id", "") or "")
        tgt_prof = self.ctx.connection_hub.get_profile(tgt_cid) if tgt_cid else None
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
        state = self.get_overview_state(
            selected_table_key=eff_key,
            force_refresh=force_refresh,
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
        )
        state["message"] = "已保存双列映射与书签绑定"
        return state

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
            comp = self.compare_single_table(
                (std_table_key or source_table or target_table).strip().lower(),
                override_source_schema=source_schema or None,
                override_source_table=source_table or None,
                override_target_schema=target_schema or None,
                override_target_table=target_table or None,
                persist_missing_binding=False,
            )
            tb = comp.get("table_binding") or {}
            s_sch = tb.get("source_schema") or source_schema or "public"
            s_tbl = tb.get("source_table") or source_table or ""
            t_sch = tb.get("target_schema") or target_schema or "public"
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
                ws.append(r)
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

        self.ctx.store.add_log(
            "INFO",
            f"三栏比对 [{eff_key}]",
            f"已持久保存 [{eff_key}] 的三方绑定关系：来源({s_sch}.{s_tbl or eff_key}) ↔ 数标 ↔ 目标({t_sch}.{t_tbl or eff_key})",
        )
        return self.get_overview_state(selected_table_key=eff_key, force_refresh=force_refresh)

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
    ) -> Dict[str, Any]:
        """经由 PlatformStateStore 数据契约将待贯标字段推送至单库贯标队列（零跨模块导入）."""
        tables = (
            [self.ctx.store.get_cross_table_binding(std_table_key)]
            if std_table_key
            else self.ctx.store.list_cross_tables()
        )
        tables = [t for t in tables if t]
        if not tables:
            raise ValueError("当前没有可推送的标准绑定表")

        tgt_conn_info = self.get_role_connection_info("target")
        tgt_db_type = str(tgt_conn_info.get("db_type") or "DWS")
        eff_tgt_fmt = self._resolve_side_format_profile(
            tgt_conn_info, target_format_profile, format_profile
        )
        items_to_push: List[Dict[str, Any]] = []
        row_idx = 1

        for t in tables:
            t_key = t["std_table_key"]
            comp = self.compare_single_table(
                t_key,
                format_profile=format_profile,
                source_format_profile=source_format_profile,
                target_format_profile=eff_tgt_fmt,
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

            cols = (comp.get("standard_panel") or {}).get("columns") or self.ctx.store.list_cross_columns(t_key)
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

        fn = self.ctx.store.get_meta("cross_standard_filename", "跨环境数标推送清单.xlsx")
        self.ctx.store.replace_all_items(items_to_push, fn, preserve_history=True)

        # 若已绑定目标库，将贯标执行台的激活连接同步指向该目标库
        tgt_cid = str(self.ctx.store.get_meta("cross_target_connection_id", "") or "")
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

        row_num = 1
        for t in tables:
            comp = self.compare_single_table(t["std_table_key"])
            t_bind = comp["table_binding"]
            for sc in comp["standard_panel"]["columns"]:
                src_m = sc.get("source_col_meta") or {}
                tgt_m = sc.get("target_col_meta") or {}
                ws.append(
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
                    ]
                )
                row_num += 1

        widths = [6, 14, 22, 14, 22, 20, 18, 22, 20, 16, 20, 22, 18, 24, 14, 22, 20, 18, 22, 16, 36]
        for idx, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = w

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
