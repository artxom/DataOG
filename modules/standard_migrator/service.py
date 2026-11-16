"""单库批量落标贯标执行台服务 (StandardMigratorService).

完整保留华为云 DWS / PostgreSQL 双模式保序迁移能力：
1. 模式 A (DIRECT_ALTER)：原生 RENAME COLUMN + ALTER COLUMN TYPE USING + COMMENT ON COLUMN + attnum 物理列序校验；
2. 模式 B (TABLE_REBUILD)：分布键/分区键/列存表整表按 attnum 物理顺序影子表重建、Kahn 拓扑排序与不动点冲突剔除、period 起始分区继承；
3. 全面接入全局 ConnectionHub 与 MetadataHub，通过 connection_id 零配置共享数据源。
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.dws_engine import DWSMigratorEngine
from core.platform.context import PlatformContext
from core.platform.dialects import clean_identifier_input
from core.platform.excel_io import (
    export_report_excel,
    generate_template_excel,
    parse_excel_checklist,
    validate_xlsx_bytes,
)


class StandardMigratorService:
    """单库贯标执行台核心业务服务."""

    def __init__(self, ctx: PlatformContext, engine: Optional[DWSMigratorEngine] = None) -> None:
        self.ctx = ctx
        self.engine = engine or DWSMigratorEngine(ctx.store)

    def get_active_connection_id(self) -> str:
        cid = str(self.ctx.store.get_meta("migrator_connection_id", "") or "")
        if cid and self.ctx.connection_hub.get_profile(cid):
            return cid
        for p in self.ctx.connection_hub.list_profiles():
            if p["db_type"] in ("DWS", "PGSQL", "KINGBASE"):
                self.ctx.store.save_meta("migrator_connection_id", p["connection_id"])
                return p["connection_id"]
        profiles = self.ctx.connection_hub.list_profiles()
        if profiles:
            self.ctx.store.save_meta("migrator_connection_id", profiles[0]["connection_id"])
            return profiles[0]["connection_id"]
        return ""

    def _sync_engine_with_active_profile(self) -> Optional[Any]:
        cid = self.get_active_connection_id()
        if not cid:
            return None
        prof = self.ctx.connection_hub.get_profile(cid)
        if prof and prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host:
            self.engine._apply_profile_to_conn_config(prof)
        return prof

    def get_workspace_state(self) -> Dict[str, Any]:
        prof = self._sync_engine_with_active_profile()
        cid = prof.connection_id if prof else ""
        conn_public = prof.to_public_dict() if prof else None
        catalog_tree = (
            self.ctx.metadata_hub.get_catalog_tree(cid, auto_refresh=False)
            if cid
            else {"schemas": [], "tables": []}
        )
        return {
            "ok": True,
            "active_connection_id": cid,
            "active_connection": conn_public,
            "connections": self.ctx.connection_hub.list_profiles(),
            "catalog": catalog_tree,
            "excel_name": self.ctx.store.get_meta("current_excel_name", "未导入"),
            "imported_at": self.ctx.store.get_meta("imported_at", ""),
            "summary": self.ctx.store.get_summary(),
            "items": self.ctx.store.list_items(),
            "logs": self.ctx.store.list_logs(limit=150),
        }

    def bind_connection(self, connection_id: str, auto_sync: bool = True) -> Dict[str, Any]:
        prof = self.ctx.connection_hub.require_profile(connection_id)
        self.ctx.store.save_meta("migrator_connection_id", prof.connection_id)
        if prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host:
            self.engine._apply_profile_to_conn_config(prof)
            self.ctx.store.save_meta(
                "dws_conn_saved",
                {
                    "configured": True,
                    "host": prof.host,
                    "port": prof.port,
                    "dbname": prof.dbname,
                    "user": prof.user,
                    "password": prof.password,
                    "sslmode": prof.sslmode,
                },
            )

        sync_warning = ""
        if auto_sync:
            try:
                self.ctx.metadata_hub.refresh_catalog(prof.connection_id)
                if self.ctx.store.list_items():
                    self.sync_items()
            except Exception as exc:
                sync_warning = f"数据源已切换，但实时同步元数据未完成: {exc}"

        state = self.get_workspace_state()
        if sync_warning:
            state["warning"] = sync_warning
        state["message"] = f"贯标执行台已绑定数据源 [{prof.name}]"
        return state

    def sync_items(self) -> Dict[str, Any]:
        prof = self._sync_engine_with_active_profile()
        if not prof:
            raise ValueError("尚未绑定贯标目标数据源连接，请先在顶部选择连接档案")

        items = self.ctx.store.list_items()
        if not items:
            return self.get_workspace_state()

        if prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host and prof.driver_track == "NATIVE":
            self.engine.sync_all_items_with_db()
        else:
            # 跨任意驱动（含 SQLite / MySQL / 自定义 JDBC）通过全局 MetadataHub 进行字段合规核验
            updates: List[Dict[str, Any]] = []
            meta_cache: Dict[tuple[str, str], Any] = {}
            for it in items:
                key = (str(it["schema_name"]).lower(), str(it["table_name"]).lower())
                if key not in meta_cache:
                    meta_cache[key] = self.ctx.metadata_hub.get_table_metadata(
                        prof.connection_id,
                        schema_name=it["schema_name"],
                        table_name=it["table_name"],
                        force_refresh=True,
                    )
                upd = self.engine._build_single_item_sync_update(it, meta_cache[key])
                updates.append(upd)
            self.ctx.store.batch_update_items_sync(updates)

        state = self.get_workspace_state()
        state["message"] = f"已完成 {len(items)} 条字段规则的库内元数据实时比对核验"
        return state

    def upload_excel_checklist(
        self,
        filename: str,
        content: bytes,
        default_schema: str = "",
        default_table: str = "",
    ) -> Dict[str, Any]:
        validate_xlsx_bytes(filename, content)
        prof = self._sync_engine_with_active_profile()
        eff_schema = (
            (default_schema or "").strip()
            or (prof.default_schema.strip() if prof and prof.default_schema else "")
            or "public"
        )
        parsed_items = parse_excel_checklist(
            content,
            filename,
            default_schema=eff_schema,
            default_table=(default_table or "").strip(),
        )
        self.ctx.store.replace_all_items(parsed_items, filename, preserve_history=True)
        self.ctx.store.replace_cross_standards(
            parsed_items, filename, preserve_bindings=True
        )

        sync_warning = ""
        if prof:
            try:
                self.sync_items()
            except Exception as exc:
                sync_warning = f"清单已导入，但自动连接数据库核验未完成: {exc}"

        state = self.get_workspace_state()
        if sync_warning:
            state["warning"] = sync_warning
        state["message"] = f"成功导入清单 [{filename}]，共解析 {len(parsed_items)} 条字段规则"
        return state

    def load_tables_from_db(
        self,
        *,
        connection_id: Optional[str] = None,
        schema_name: str = "",
        table_name: str = "",
        max_tables: int = 30,
    ) -> Dict[str, Any]:
        if connection_id:
            self.bind_connection(connection_id, auto_sync=False)
        prof = self._sync_engine_with_active_profile()
        if not prof:
            raise ValueError("请先选择有效的目标数据源连接")

        if prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host and prof.driver_track == "NATIVE":
            cnt = self.engine.load_tables_from_database(
                schema_name=schema_name,
                table_name=table_name,
                max_tables=max_tables,
            )
        else:
            tree = self.ctx.metadata_hub.refresh_catalog(
                prof.connection_id, schema_name=schema_name or None
            )
            matched_tables = [
                t
                for t in tree["tables"]
                if t.get("object_type", "TABLE") == "TABLE"
                and (not schema_name or t["schema_name"].lower() == schema_name.strip().lower())
                and (not table_name or t["table_name"].lower() == table_name.strip().lower())
            ][:max_tables]
            loaded_items: List[Dict[str, Any]] = []
            for t in matched_tables:
                t_meta = self.ctx.metadata_hub.get_table_metadata(
                    prof.connection_id, t["schema_name"], t["table_name"], force_refresh=True
                )
                for c in t_meta.columns:
                    loaded_items.append(
                        {
                            "row_index": len(loaded_items) + 1,
                            "schema_name": t_meta.schema_name,
                            "table_name": t_meta.table_name,
                            "old_col_name": c.column_name,
                            "old_col_type": c.data_type,
                            "old_col_comment": c.column_comment,
                            "new_col_name": c.column_name,
                            "new_col_type": c.data_type,
                            "new_col_comment": c.column_comment,
                        }
                    )
            cnt = len(loaded_items)
            if loaded_items:
                label = f"{prof.name} [{schema_name or '全部模式'}.{table_name or f'共{len(matched_tables)}张表'}]"
                self.ctx.store.replace_all_items(loaded_items, label, preserve_history=False)
                self.sync_items()

        state = self.get_workspace_state()
        state["loaded_count"] = cnt
        state["message"] = (
            f"已从 [{prof.name}] 载入 {cnt} 个表字段结构至执行台"
            if cnt > 0
            else "未在目标数据源下匹配到符合条件的物理表"
        )
        return state

    def execute_single(self, item_id: int) -> Dict[str, Any]:
        self._sync_engine_with_active_profile()
        item = self.engine.execute_single_item_direct(item_id)
        state = self.get_workspace_state()
        state["item"] = item
        return state

    def execute_batch(
        self, item_ids: List[int], max_workers: int = 5, keep_backup: bool = True
    ) -> Dict[str, Any]:
        self._sync_engine_with_active_profile()
        batch_res = self.engine.execute_batch_items(
            item_ids=item_ids, max_workers=max_workers, keep_backup=keep_backup
        )
        state = self.get_workspace_state()
        state["batch_result"] = batch_res
        state["message"] = batch_res.get("message", "批量贯标任务执行完毕")
        return state

    def preview_rebuild(
        self,
        schema_name: str,
        table_name: str,
        item_ids: Optional[List[int]] = None,
        keep_backup: bool = True,
    ) -> Dict[str, Any]:
        prof = self._sync_engine_with_active_profile()
        all_items = self.ctx.store.list_items()
        if item_ids:
            id_set = {int(x) for x in item_ids}
            tbl_items = [it for it in all_items if it["id"] in id_set]
        else:
            tbl_items = [
                it
                for it in all_items
                if it["schema_name"].lower() == schema_name.lower()
                and it["table_name"].lower() == table_name.lower()
                and it["status"] != "COMPLETED"
            ]

        meta_override = None
        if prof and not (
            prof.db_type in ("DWS", "PGSQL", "KINGBASE")
            and prof.host
            and prof.driver_track == "NATIVE"
        ):
            meta_override = self.ctx.metadata_hub.get_table_metadata(
                prof.connection_id,
                schema_name=schema_name,
                table_name=table_name,
                force_refresh=False,
            )

        plan = self.engine.build_table_rebuild_sql_preview(
            schema_name,
            table_name,
            tbl_items,
            keep_backup=keep_backup,
            table_meta_override=meta_override,
        )
        t_meta_info = plan.get("table_meta") or {}
        return {
            "ok": True,
            "schema_name": plan["schema_name"],
            "table_name": plan["table_name"],
            "tmp_table_name": plan["tmp_table_name"],
            "bak_table_name": plan["bak_table_name"],
            "sql_preview": plan["sql_preview"],
            "rebuild_sql": plan["sql_preview"],
            "column_count": int(plan.get("total_columns") or 0),
            "distribute_clause": t_meta_info.get("distribute_clause", ""),
            "partition_clause": t_meta_info.get("partition_clause", ""),
            "column_order_comparison": plan.get("column_order_comparison", []),
        }

    def execute_rebuild(
        self,
        schema_name: str,
        table_name: str,
        item_ids: Optional[List[int]] = None,
        keep_backup: bool = True,
    ) -> Dict[str, Any]:
        self._sync_engine_with_active_profile()
        res = self.engine.execute_table_rebuild_migration(
            schema_name, table_name, item_ids=item_ids, keep_backup=keep_backup
        )
        state = self.get_workspace_state()
        state["rebuild_result"] = res
        state["message"] = res.get("message", "整表保序重建完成")
        return state

    def clear_items(self) -> Dict[str, Any]:
        self.ctx.store.clear_items()
        return self.get_workspace_state()

    @staticmethod
    def generate_template_bytes() -> bytes:
        return generate_template_excel()

    def export_report_bytes(self) -> bytes:
        return export_report_excel(self.ctx.store.list_items())

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

    def get_bindings_overview(
        self,
        connection_id: str = "",
        format_profile: str = "AUTO",
    ) -> Dict[str, Any]:
        prof = self._resolve_binding_profile(connection_id)
        cid = prof.connection_id if prof else ""
        if prof and prof.connection_id != self.ctx.store.get_meta("migrator_connection_id", ""):
            # 允许查看不同连接时不强制破坏历史执行台绑定，若当前未绑定则顺便绑定
            if not self.ctx.store.get_meta("migrator_connection_id", ""):
                self.ctx.store.save_meta("migrator_connection_id", prof.connection_id)

        catalog_tree = (
            self.ctx.metadata_hub.get_catalog_tree(cid, auto_refresh=False)
            if cid
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
            "format_profile": format_profile or "AUTO",
            "table_stats": self.ctx.standard_catalog.get_connection_binding_stats(cid) if cid else {},
            "migration_summary": self.ctx.store.get_summary(),
        }

    def get_table_compliance(
        self,
        connection_id: str,
        schema_name: str,
        table_name: str,
        *,
        format_profile: str = "AUTO",
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        prof = self.ctx.connection_hub.require_profile(connection_id)
        eff_schema = (schema_name or prof.default_schema or "public").strip()
        eff_table = (table_name or "").strip()
        if "." in eff_table and not schema_name:
            eff_schema, eff_table = eff_table.split(".", 1)
        if not eff_table:
            raise ValueError("表名不能为空")

        t_meta = self.ctx.metadata_hub.get_table_metadata(
            prof.connection_id,
            schema_name=eff_schema,
            table_name=eff_table,
            force_refresh=force_refresh,
            penetrate_views=True,
        )
        actual_schema = t_meta.schema_name or eff_schema
        actual_table = t_meta.table_name or eff_table

        bindings_map = self.ctx.standard_catalog.get_table_field_bindings(
            prof.connection_id, actual_schema, actual_table
        )
        std_codes = [
            b.get("std_code", "")
            for b in bindings_map.values()
            if (b.get("std_code") or "").strip()
        ]
        std_dict_map = self.ctx.standard_catalog.get_items_by_codes(std_codes)

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
            if std_code and not std_item:
                std_item = self.ctx.standard_catalog.get_item(std_code)
                if std_item:
                    std_dict_map[std_code] = std_item

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
                compliance_updates.append(
                    {
                        "schema_name": actual_schema,
                        "table_name": actual_table,
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
                (is_dist or is_part or is_col_store)
                and prof.db_type in ("DWS", "PGSQL", "KINGBASE")
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

        if compliance_updates:
            self.ctx.standard_catalog.update_binding_compliance_batch(
                prof.connection_id, compliance_updates
            )

        total_c = counts["total_cols"]
        counts["coverage_pct"] = (
            round(counts["bound_cols"] * 100.0 / total_c, 1) if total_c > 0 else 0.0
        )
        counts["compliant_pct"] = (
            round(counts["compliant_cols"] * 100.0 / total_c, 1) if total_c > 0 else 0.0
        )

        return {
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
            "table_stats": self.ctx.standard_catalog.get_connection_binding_stats(prof.connection_id),
        }

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
        self.ctx.standard_catalog.upsert_field_bindings(
            prof.connection_id, bindings, bind_source="MANUAL"
        )
        sch = active_schema or (bindings[0].get("schema_name", "") if bindings else "")
        tbl = active_table or (bindings[0].get("table_name", "") if bindings else "")
        if tbl:
            res = self.get_table_compliance(
                prof.connection_id,
                sch or prof.default_schema or "public",
                tbl,
                format_profile=format_profile,
                force_refresh=False,
            )
            res["message"] = f"已保存 {actual_len_text(bindings)} 条字段标准绑定"
            return res
        return {
            "ok": True,
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
    ) -> Dict[str, Any]:
        prof = self.ctx.connection_hub.require_profile(connection_id)
        targets = self._parse_table_targets(
            tables=tables,
            schema_name=schema_name,
            table_name=table_name,
            default_schema=prof.default_schema or "public",
        )
        if not targets:
            tree = self.ctx.metadata_hub.get_catalog_tree(prof.connection_id, auto_refresh=False)
            targets = [
                (t["schema_name"], t["table_name"])
                for t in (tree.get("tables") or [])[:50]
            ]
        if not targets:
            raise ValueError("当前连接暂无可匹配的数据表，请先刷新元数据或选择目标表")

        matched_count = 0
        to_upsert: List[Dict[str, Any]] = []
        for sch, tbl in targets:
            t_meta = self.ctx.metadata_hub.get_table_metadata(
                prof.connection_id, sch, tbl, force_refresh=False
            )
            existing = self.ctx.standard_catalog.get_table_field_bindings(
                prof.connection_id, t_meta.schema_name or sch, t_meta.table_name or tbl
            )
            for col in t_meta.columns:
                col_clean = clean_identifier_input(col.column_name) or col.column_name
                cur_bind = existing.get(col_clean.lower())
                if cur_bind and (cur_bind.get("std_code") or "").strip():
                    continue
                cands = self.ctx.standard_catalog.match_standard_candidates(
                    col_name=col_clean,
                    col_comment=col.column_comment or "",
                )
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
                if len(cands) == 1 or (exact_en and exact_cn) or picked.get("is_standard_dict") == "1":
                    to_upsert.append(
                        {
                            "schema_name": t_meta.schema_name or sch,
                            "table_name": t_meta.table_name or tbl,
                            "column_name": col_clean,
                            "std_code": picked["std_code"],
                        }
                    )
                    matched_count += 1

        if to_upsert:
            self.ctx.standard_catalog.upsert_field_bindings(
                prof.connection_id, to_upsert, bind_source="AUTO_MATCH"
            )

        last_table_res = None
        for sch, tbl in targets:
            last_table_res = self.get_table_compliance(
                prof.connection_id,
                sch,
                tbl,
                format_profile=format_profile,
                force_refresh=False,
            )

        res: Dict[str, Any] = {
            "ok": True,
            "matched_count": matched_count,
            "checked_tables": len(targets),
            "table_stats": self.ctx.standard_catalog.get_connection_binding_stats(prof.connection_id),
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
    ) -> Dict[str, Any]:
        prof = self.ctx.connection_hub.require_profile(connection_id)
        targets = self._parse_table_targets(
            tables=tables,
            default_schema=prof.default_schema or "public",
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

        table_results: List[Dict[str, Any]] = []
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
        for sch, tbl in targets:
            t_res = self.get_table_compliance(
                prof.connection_id,
                sch,
                tbl,
                format_profile=format_profile,
                force_refresh=force_refresh,
            )
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
        out: Dict[str, Any] = {
            "ok": True,
            "connection_id": prof.connection_id,
            "format_profile": format_profile or "AUTO",
            "aggregate": agg,
            "tables": table_results,
            "table_stats": self.ctx.standard_catalog.get_connection_binding_stats(prof.connection_id),
            "message": f"核验完成：{agg['checked_tables']} 张表共 {agg['total_cols']} 列，已落标 {agg['compliant_cols']} 列、未落标 {agg['non_compliant_cols']} 列、仅差注释 {agg['comment_only_cols']} 列",
        }
        if len(table_results) == 1:
            out["active_table"] = table_results[0]
        return out

    def generate_binding_template_bytes(
        self,
        *,
        connection_id: str = "",
        schema_name: str = "",
        tables: str = "",
    ) -> bytes:
        """生成「数据标准-表字段对照关系」系统模板（支持空白模板与按已选库表预填真实字段）."""
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
                raw_list = [x.strip() for x in (tables or "").split(",") if x.strip()]
                targets = self._parse_table_targets(
                    tables=raw_list,
                    schema_name=schema_name,
                    default_schema=prof.default_schema or "public",
                )
                for sch, tbl in targets:
                    t_meta = self.ctx.metadata_hub.get_table_metadata(
                        prof.connection_id, sch, tbl, force_refresh=False
                    )
                    binds = self.ctx.standard_catalog.get_table_field_bindings(
                        prof.connection_id, t_meta.schema_name or sch, t_meta.table_name or tbl
                    )
                    std_map = self.ctx.standard_catalog.get_items_by_codes(
                        [b.get("std_code", "") for b in binds.values()]
                    )
                    for col in t_meta.columns:
                        col_clean = clean_identifier_input(col.column_name) or col.column_name
                        b = binds.get(col_clean.lower())
                        code = (b.get("std_code") if b else "") or ""
                        s_item = std_map.get(code) if code else None
                        prefilled_rows.append(
                            [
                                t_meta.schema_name or sch,
                                t_meta.table_name or tbl,
                                col_clean,
                                col.data_type or "",
                                col.column_comment or "",
                                code,
                                s_item.get("en_abbr", "") if s_item else "",
                                s_item.get("cn_name", "") if s_item else "",
                            ]
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
    ) -> Dict[str, Any]:
        prof = self.ctx.connection_hub.require_profile(connection_id)
        validate_xlsx_bytes(filename, content)

        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        try:
            all_sheets_rows: List[List[Any]] = []
            for ws in wb.worksheets:
                t_norm = (ws.title or "").strip().lower()
                if t_norm in ("填写说明", "填表说明", "使用说明", "说明", "目录", "封面", "readme"):
                    continue
                sheet_rows = list(ws.iter_rows(values_only=True))
                if sheet_rows:
                    all_sheets_rows.append(sheet_rows)
        finally:
            wb.close()

        if not all_sheets_rows:
            raise ValueError("对照关系表为空，请下载系统模板后填写再上传")

        def _norm(v: Any) -> str:
            return (
                str(v or "")
                .strip()
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
            "std_code": {"数据项编号", "标准编号", "数标编号", "stdcode"},
            "std_en_abbr": {"标准英文简称(选填)", "标准英文简称", "英文简称", "标准字段名", "enabbr"},
            "std_cn_name": {"标准中文名称(选填)", "标准中文名称", "标准中文名", "中文名称", "cnname"},
        }

        rows: List[Any] = []
        header_idx = -1
        col_map: Dict[str, int] = {}
        for candidate_rows in all_sheets_rows:
            for r_idx, row in enumerate(candidate_rows[:15]):
                normed = [_norm(c) for c in row]
                candidate: Dict[str, int] = {}
                for k, aliases in alias_map.items():
                    for c_i, cell_t in enumerate(normed):
                        if cell_t in aliases and k not in candidate:
                            candidate[k] = c_i
                            break
                if "table_name" in candidate and "column_name" in candidate:
                    header_idx = r_idx
                    col_map = candidate
                    rows = candidate_rows
                    break
            if header_idx != -1:
                break

        if header_idx == -1 or len(rows) < 2:
            raise ValueError("未识别到有效对照表头（需包含「表名」「物理字段名」「数据项编号」等列），请下载系统模板对照")

        eff_default_schema = (default_schema or prof.default_schema or "public").strip()
        parsed_bindings: List[Dict[str, Any]] = []
        unresolved_rows: List[str] = []
        touched_tables: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

        for row_no, row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
            if not row or all(x is None or str(x).strip() == "" for x in row):
                continue

            def _cell(k: str) -> str:
                ci = col_map.get(k)
                if ci is None or ci >= len(row) or row[ci] is None:
                    return ""
                return str(row[ci]).strip()

            sch = clean_identifier_input(_cell("schema_name")) or eff_default_schema
            tbl = clean_identifier_input(_cell("table_name"))
            col = clean_identifier_input(_cell("column_name"))
            if "." in tbl and not _cell("schema_name"):
                sch, tbl = tbl.split(".", 1)
            if not tbl or not col:
                continue

            raw_code = _cell("std_code")
            std_en = clean_identifier_input(_cell("std_en_abbr"))
            std_cn = _cell("std_cn_name")
            matched_code = raw_code
            if not matched_code and (std_en or std_cn):
                cands = self.ctx.standard_catalog.match_standard_candidates(
                    col_name=std_en or col,
                    col_comment=std_cn,
                )
                if cands:
                    matched_code = cands[0]["std_code"]
            if not matched_code:
                unresolved_rows.append(f"第{row_no}行 {sch}.{tbl}.{col}")
                continue

            entry = {
                "schema_name": sch,
                "table_name": tbl,
                "column_name": col,
                "std_code": matched_code,
            }
            parsed_bindings.append(entry)
            touched_tables.setdefault((sch, tbl), []).append(entry)

        if not parsed_bindings:
            raise ValueError("未解析到包含有效数据项编号或匹配标准的对照行")

        if (mode or "merge").strip().lower() == "replace_table":
            for (sch, tbl), rows_for_tbl in touched_tables.items():
                self.ctx.standard_catalog.replace_table_field_bindings(
                    prof.connection_id, sch, tbl, rows_for_tbl, bind_source="EXCEL"
                )
        else:
            self.ctx.standard_catalog.upsert_field_bindings(
                prof.connection_id, parsed_bindings, bind_source="EXCEL"
            )

        targets = [{"schema_name": sch, "table_name": tbl} for sch, tbl in touched_tables.keys()]
        check_res = self.check_compliance(
            connection_id=prof.connection_id,
            tables=targets,
            format_profile=format_profile,
            force_refresh=False,
        )
        msg = f"成功导入 {len(parsed_bindings)} 条字段标准对照关系（涉及 {len(touched_tables)} 张表）"
        if unresolved_rows:
            msg += f"，另有 {len(unresolved_rows)} 行未填写编号且未匹配到标准"
        check_res["imported_count"] = len(parsed_bindings)
        check_res["unresolved_count"] = len(unresolved_rows)
        check_res["message"] = msg
        return check_res

    def export_binding_report_bytes(
        self,
        *,
        connection_id: str = "",
        format_profile: str = "AUTO",
        tables: str = "",
    ) -> bytes:
        prof = self._resolve_binding_profile(connection_id)
        if not prof:
            raise ValueError("请先绑定目标数据源连接")

        raw_list = [x.strip() for x in (tables or "").split(",") if x.strip()]
        targets = self._parse_table_targets(
            tables=raw_list,
            default_schema=prof.default_schema or "public",
        )
        if not targets:
            targets = self.ctx.standard_catalog.list_bound_tables_for_connection(prof.connection_id)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "落标核验报告"
        headers = [
            "序号",
            "模式名",
            "表名",
            "列序",
            "库内字段名",
            "库内数据类型",
            "库内注释",
            "数据项编号",
            "标准中文名称",
            "标准英文简称",
            "标准目标类型",
            "落标状态",
            "差异说明",
        ]
        ws.append(headers)

        fills = {
            "COMPLIANT": PatternFill(start_color="E6F4EA", end_color="E6F4EA", fill_type="solid"),
            "COMMENT_ONLY": PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid"),
            "NON_COMPLIANT": PatternFill(start_color="FCE8E6", end_color="FCE8E6", fill_type="solid"),
            "STD_MISSING": PatternFill(start_color="FEF7E0", end_color="FEF7E0", fill_type="solid"),
        }
        seq = 1
        for sch, tbl in targets:
            comp = self.get_table_compliance(
                prof.connection_id,
                sch,
                tbl,
                format_profile=format_profile,
                force_refresh=False,
            )
            for c in comp["columns"]:
                st = c["compliance_status"]
                ws.append(
                    [
                        seq,
                        comp["table"]["schema_name"],
                        comp["table"]["table_name"],
                        c["ordinal_position"],
                        c["column_name"],
                        c["data_type"],
                        c["column_comment"],
                        c["std_code"],
                        c["std_cn_name"],
                        c["std_en_abbr"],
                        c["std_target_type"],
                        c["compliance_label"],
                        c["compliance_detail"],
                    ]
                )
                fill = fills.get(st)
                if fill:
                    r_idx = ws.max_row
                    for c_i in range(1, len(headers) + 1):
                        ws.cell(row=r_idx, column=c_i).fill = fill
                seq += 1

        header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
        for c_i in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=c_i)
            cell.fill = header_fill
            cell.font = header_font
            ws.column_dimensions[get_column_letter(c_i)].width = 18
        ws.column_dimensions["M"].width = 36
        ws.freeze_panes = "A2"

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def stage_migration_from_bindings(
        self,
        *,
        connection_id: str,
        schema_name: str,
        table_name: str,
        column_names: Optional[List[str]] = None,
        format_profile: str = "AUTO",
        append: bool = False,
    ) -> Dict[str, Any]:
        """将已绑定数标的未达标字段生成贯标改造项，无缝衔接单条 ALTER 或 TABLE_REBUILD 保序重建."""
        comp = self.get_table_compliance(
            connection_id,
            schema_name,
            table_name,
            format_profile=format_profile,
            force_refresh=False,
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

            new_items.append(
                {
                    "row_index": len(new_items) + 1,
                    "schema_name": actual_sch,
                    "table_name": actual_tbl,
                    "old_col_name": col["column_name"],
                    "old_col_type": col["data_type"],
                    "old_col_comment": col["column_comment"],
                    "new_col_name": col["std_en_abbr"] or col["column_name"],
                    "new_col_type": col["std_target_type"] or col["data_type"],
                    "new_col_comment": col["std_cn_name"] or col["column_comment"],
                }
            )

        if not new_items:
            raise ValueError("所选字段暂无需要贯标改造的已绑标条目（或均已达标）")

        self.ctx.store.save_meta("migrator_connection_id", connection_id)
        self._sync_engine_with_active_profile()

        merged_items = list(new_items)
        if append:
            existing = self.ctx.store.list_items()
            kept = [
                {
                    "schema_name": it["schema_name"],
                    "table_name": it["table_name"],
                    "old_col_name": it["old_col_name"],
                    "old_col_type": it["old_col_type"],
                    "old_col_comment": it["old_col_comment"],
                    "new_col_name": it["new_col_name"],
                    "new_col_type": it["new_col_type"],
                    "new_col_comment": it["new_col_comment"],
                }
                for it in existing
                if not (
                    it["schema_name"].lower() == actual_sch.lower()
                    and it["table_name"].lower() == actual_tbl.lower()
                )
            ]
            merged_items = kept + new_items

        for idx, it in enumerate(merged_items, start=1):
            it["row_index"] = idx

        label = f"数标落标生成 [{actual_sch}.{actual_tbl}]"
        self.ctx.store.replace_all_items(merged_items, label, preserve_history=True)
        self.sync_items()

        state = self.get_workspace_state()
        state["staged_count"] = len(new_items)
        state["message"] = f"已为 {actual_sch}.{actual_tbl} 生成 {len(new_items)} 条贯标改造任务，可执行 ALTER 或预览保序重建"
        return state


def actual_len_text(items: List[Any]) -> str:
    return str(len(items or []))
