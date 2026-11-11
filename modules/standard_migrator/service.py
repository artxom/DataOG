"""单库批量落标贯标执行台服务 (StandardMigratorService).

完整保留华为云 DWS / PostgreSQL 双模式保序迁移能力：
1. 模式 A (DIRECT_ALTER)：原生 RENAME COLUMN + ALTER COLUMN TYPE USING + COMMENT ON COLUMN + attnum 物理列序校验；
2. 模式 B (TABLE_REBUILD)：分布键/分区键/列存表整表按 attnum 物理顺序影子表重建、Kahn 拓扑排序与不动点冲突剔除、period 起始分区继承；
3. 全面接入全局 ConnectionHub 与 MetadataHub，通过 connection_id 零配置共享数据源。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.dws_engine import DWSMigratorEngine
from core.platform.context import PlatformContext
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
