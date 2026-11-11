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
from core.platform.excel_io import parse_excel_checklist, validate_xlsx_bytes
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
        self, selected_table_key: Optional[str] = None, force_refresh: bool = False
    ) -> Dict[str, Any]:
        tables = self.ctx.store.list_cross_tables()
        if not tables and self.ctx.store.list_items():
            fn = self.ctx.store.get_meta("current_excel_name", "执行台同步清单.xlsx")
            tables = self.ctx.store.replace_cross_standards(
                self.ctx.store.list_items(), fn, preserve_bindings=True
            )

        active_key = (selected_table_key or "").strip().lower()
        if not active_key and tables:
            active_key = tables[0]["std_table_key"]

        comp = (
            self.compare_single_table(active_key, force_refresh=force_refresh)
            if active_key
            else None
        )
        return {
            "ok": True,
            "connections": self.ctx.connection_hub.list_profiles(),
            "source_connection": self.get_role_connection_info("source"),
            "target_connection": self.get_role_connection_info("target"),
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
    ) -> Dict[str, Any]:
        t_bind = self.ctx.store.get_cross_table_binding(std_table_key)
        if not t_bind:
            t_bind = self.ctx.store.upsert_cross_table_binding(
                std_table_key,
                source_schema=override_source_schema or "public",
                source_table=override_source_table or std_table_key,
                target_schema=override_target_schema or "public",
                target_table=override_target_table or std_table_key,
            )

        src_conn = self.get_role_connection_info("source")
        tgt_conn = self.get_role_connection_info("target")

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

        std_cols = self.ctx.store.list_cross_columns(t_bind["std_table_key"])
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

        return {
            "table_binding": {
                **t_bind,
                "source_schema": src_meta.schema_name if src_meta.exists else src_schema,
                "source_table": src_meta.table_name if src_meta.exists else src_table,
                "target_schema": tgt_meta.schema_name if tgt_meta.exists else tgt_schema,
                "target_table": tgt_meta.table_name if tgt_meta.exists else tgt_table,
            },
            "summary": summary,
            "source_panel": {
                "configured": bool(src_conn.get("configured")),
                "connection_id": src_conn.get("connection_id", ""),
                "connection_name": src_conn.get("name", ""),
                "db_type": src_conn.get("db_type", "DWS"),
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

    def push_to_migrator(self, std_table_key: Optional[str] = None) -> Dict[str, Any]:
        """经由 PlatformStateStore 数据契约将待贯标字段推送至单库贯标队列（零跨模块导入）."""
        tables = (
            [self.ctx.store.get_cross_table_binding(std_table_key)]
            if std_table_key
            else self.ctx.store.list_cross_tables()
        )
        tables = [t for t in tables if t]
        if not tables:
            raise ValueError("当前没有可推送的标准绑定表")

        items_to_push: List[Dict[str, Any]] = []
        row_idx = 1
        for t in tables:
            t_key = t["std_table_key"]
            comp = self.compare_single_table(t_key)
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

    def import_standard_or_bundle(self, filename: str, content: bytes) -> Dict[str, Any]:
        lower_fn = (filename or "").strip().lower()
        if lower_fn.endswith(".json"):
            bundle = json.loads(content.decode("utf-8"))
            res = self.ctx.store.import_cross_bindings_bundle(bundle, filename=filename)
            self.ctx.store.add_log(
                "SUCCESS",
                "数标导入",
                f"成功从 JSON 绑定包 [{filename}] 恢复 {res['table_count']} 张表、{res['column_count']} 条字段绑定关系",
            )
            return self.get_overview_state()

        validate_xlsx_bytes(filename, content)
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) >= 2:
            header_cells = [str(c or "").strip() for c in rows[0]]
            if "数据标准字段名" in header_cells and "来源库绑定字段" in header_cells:
                idx_map = {h: i for i, h in enumerate(header_cells)}
                items: List[Dict[str, Any]] = []
                for r in rows[1:]:
                    if not r or all(x is None or str(x).strip() == "" for x in r):
                        continue
                    std_tbl = str(r[idx_map.get("标准表名", 2)] or "").strip()
                    std_col = str(r[idx_map.get("数据标准字段名", 11)] or "").strip()
                    if not std_tbl or not std_col:
                        continue
                    items.append(
                        {
                            "schema_name": str(r[idx_map.get("标准模式名", 1)] or "public").strip(),
                            "table_name": std_tbl,
                            "source_schema": str(r[idx_map.get("来源库模式", 3)] or "").strip(),
                            "source_table": str(r[idx_map.get("来源库表名", 4)] or std_tbl).strip(),
                            "bound_source_col": str(r[idx_map.get("来源库绑定字段", 5)] or "").strip(),
                            "old_col_name": str(r[idx_map.get("清单原字段名", 8)] or "").strip(),
                            "old_col_type": str(r[idx_map.get("清单原类型", 9)] or "").strip(),
                            "old_col_comment": str(r[idx_map.get("清单原注释", 10)] or "").strip(),
                            "new_col_name": std_col,
                            "new_col_type": str(r[idx_map.get("数据标准类型", 12)] or "").strip(),
                            "new_col_comment": str(r[idx_map.get("数据标准注释", 13)] or "").strip(),
                            "target_schema": str(r[idx_map.get("目标库模式", 14)] or "").strip(),
                            "target_table": str(r[idx_map.get("目标库表名", 15)] or std_tbl).strip(),
                            "bound_target_col": str(r[idx_map.get("目标库绑定字段", 16)] or "").strip(),
                        }
                    )
                if items:
                    self.ctx.store.replace_cross_standards(items, filename, preserve_bindings=False)
                    return self.get_overview_state()

        parsed_items = parse_excel_checklist(content, filename)
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
