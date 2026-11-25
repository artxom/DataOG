"""单库批量落标贯标执行台服务 (StandardMigratorService).

完整保留华为云 DWS / PostgreSQL 双模式保序迁移能力：
1. 模式 A (DIRECT_ALTER)：原生 RENAME COLUMN + ALTER COLUMN TYPE USING + COMMENT ON COLUMN + attnum 物理列序校验；
2. 模式 B (TABLE_REBUILD)：分布键/分区键/列存表整表按 attnum 物理顺序影子表重建、Kahn 拓扑排序与不动点冲突剔除、period 起始分区继承；
3. 全面接入全局 ConnectionHub 与 MetadataHub，通过 connection_id 零配置共享数据源。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from core.dws_engine import (
    ColumnMeta as EngineColumnMeta,
    DWSMigratorEngine,
    TableMeta as EngineTableMeta,
    analyze_item_changes,
    item_requires_table_rebuild,
)
from core.platform.context import PlatformContext
from core.platform.dialects import clean_identifier_input
from core.platform.excel_io import (
    export_report_excel,
    generate_template_excel,
    parse_excel_checklist,
    validate_xlsx_bytes,
)
from core.platform.import_progress import ProgressCallback, noop_progress
from modules.standard_migrator.bindings_mixin import _StandardBindingsMixin
from modules.standard_migrator.report_mixin import _StandardReportMixin


class StandardMigratorService(_StandardBindingsMixin, _StandardReportMixin):
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
            if getattr(self.ctx.store, "is_split_storage", False):
                with self.engine._conn_lock:
                    self.engine._conn_config["configured"] = False
                    self.engine._conn_config["host"] = ""
                    self.engine._conn_config["dbname"] = ""
                    self.engine._conn_config["user"] = ""
                    self.engine._conn_config["password"] = ""
            return None
        prof = self.ctx.connection_hub.get_profile(cid)
        if prof and prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host:
            self.engine._apply_profile_to_conn_config(prof)
        elif getattr(self.ctx.store, "is_split_storage", False):
            with self.engine._conn_lock:
                self.engine._conn_config["configured"] = False
        return prof

    def _resolve_item_connection_id(self, item: Optional[Dict[str, Any]]) -> str:
        cid = str((item or {}).get("connection_id") or "").strip()
        if cid and self.ctx.connection_hub.get_profile(cid):
            return cid
        return self.get_active_connection_id()

    def _get_engine_for_connection(
        self, connection_id: str
    ) -> Tuple[DWSMigratorEngine, Optional[Any]]:
        active_cid = self.get_active_connection_id()
        eff_cid = (connection_id or "").strip() or active_cid
        if not eff_cid:
            self._sync_engine_with_active_profile()
            return self.engine, None
        prof = self.ctx.connection_hub.get_profile(eff_cid)
        if eff_cid == active_cid or not active_cid:
            if prof and prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host:
                self.engine._apply_profile_to_conn_config(prof)
            elif getattr(self.ctx.store, "is_split_storage", False):
                with self.engine._conn_lock:
                    self.engine._conn_config["configured"] = False
            return self.engine, prof
        eng = DWSMigratorEngine(self.ctx.store)
        if prof and prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host:
            eng._apply_profile_to_conn_config(prof)
        return eng, prof

    @staticmethod
    def _to_engine_table_meta(t_meta: Any) -> EngineTableMeta:
        return EngineTableMeta(
            schema_name=t_meta.schema_name,
            table_name=t_meta.table_name,
            exists=bool(t_meta.exists),
            table_comment=str(t_meta.table_comment or ""),
            orientation=str(t_meta.orientation or "ROW"),
            with_clause=str(t_meta.with_clause or ""),
            distribute_clause=str(t_meta.distribute_clause or ""),
            distribute_keys=list(t_meta.distribute_keys or []),
            partition_clause=str(t_meta.partition_clause or ""),
            partition_keys=list(t_meta.partition_keys or []),
            columns=[
                EngineColumnMeta(
                    ordinal_position=int(c.ordinal_position),
                    column_name=str(c.column_name),
                    data_type=str(c.data_type or ""),
                    is_not_null=bool(c.is_not_null),
                    column_default=c.column_default,
                    column_comment=str(c.column_comment or ""),
                    lineage_source=str(getattr(c, "lineage_source", "") or ""),
                )
                for c in (t_meta.columns or [])
            ],
            object_type=str(t_meta.object_type or "TABLE"),
            view_definition=str(t_meta.view_definition or ""),
            penetrated_base_tables=list(getattr(t_meta, "penetrated_base_tables", None) or []),
        )

    def get_workspace_state(self, *, include_catalog: bool = False) -> Dict[str, Any]:
        prof = self._sync_engine_with_active_profile()
        cid = prof.connection_id if prof else ""
        conn_public = prof.to_public_dict() if prof else None
        if cid:
            self.ctx.standard_catalog.purge_invalid_error_bindings(cid)
        catalog_tree = (
            self.ctx.metadata_hub.get_catalog_tree(cid, auto_refresh=False)
            if (include_catalog and cid)
            else {"schemas": [], "tables": []}
        )
        items = self.ctx.store.list_items()
        return {
            "ok": True,
            "active_connection_id": cid,
            "active_connection": conn_public,
            "connections": self.ctx.connection_hub.list_profiles(),
            "catalog": catalog_tree,
            "excel_name": self.ctx.store.get_meta("current_excel_name", "未导入"),
            "imported_at": self.ctx.store.get_meta("imported_at", ""),
            "summary": self.ctx.store.get_summary(items=items),
            "items": items,
            "logs": self.ctx.store.list_logs(limit=150),
        }

    def bind_connection(
        self,
        connection_id: str,
        auto_sync: bool = True,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        report(16.0, "绑定数据源", f"正在校验数据源档案 [{connection_id}]...", 0, 1)
        prof = self.ctx.connection_hub.require_profile(connection_id)
        self.ctx.store.save_meta("migrator_connection_id", prof.connection_id)
        if prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host:
            self.engine._apply_profile_to_conn_config(prof)
            if not getattr(self.ctx.store, "is_split_storage", False):
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
        report(24.0, "绑定数据源", f"已绑定数据源 [{prof.name}] ({prof.db_type})", 0, 1)

        sync_warning = ""
        if auto_sync:
            try:
                report(30.0, "同步目录缓存", f"正在刷新 [{prof.name}] 元数据目录缓存...", 0, 1)
                self.ctx.metadata_hub.refresh_catalog(
                    prof.connection_id,
                    progress_cb=lambda pct, stg, dtl, proc=0, tot=0: report(
                        30.0 + (min(100.0, max(0.0, float(pct))) / 100.0) * 35.0,
                        stg or "同步目录缓存",
                        dtl,
                        proc,
                        tot,
                    ),
                )
                if self.ctx.store.list_items():
                    report(68.0, "核验目标库状态", f"正在比对贯标队列与 [{prof.name}] 库表结构...", 0, 1)
                    self.sync_items(
                        progress_cb=lambda pct, stg, dtl, proc=0, tot=0: report(
                            68.0 + (min(100.0, max(0.0, float(pct))) / 100.0) * 28.0,
                            stg or "核验目标库状态",
                            dtl,
                            proc,
                            tot,
                        )
                    )
            except Exception as exc:
                sync_warning = f"数据源已切换，但实时同步元数据未完成: {exc}"

        state = self.get_workspace_state()
        if sync_warning:
            state["warning"] = sync_warning
        state["message"] = f"贯标执行台已绑定数据源 [{prof.name}]"
        return state

    def sync_items(
        self,
        progress_cb: Optional[ProgressCallback] = noop_progress,
        *,
        progress_start_pct: float = 20.0,
        tolerate_conn_errors: bool = False,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        items = self.ctx.store.list_items()
        if not items:
            self._sync_engine_with_active_profile()
            report(95.0, "核验目标库状态", "当前贯标队列为空，无需比对", 0, 0)
            return self.get_workspace_state()

        # 按所属连接分组，支持跨多表/多数据源的全局队列实时比对
        conn_groups: Dict[str, List[Dict[str, Any]]] = {}
        for it in items:
            cid = self._resolve_item_connection_id(it)
            conn_groups.setdefault(cid, []).append(it)

        resolved_groups: List[Tuple[str, DWSMigratorEngine, Any, List[Dict[str, Any]]]] = []
        for cid, grp in conn_groups.items():
            eng, prof = self._get_engine_for_connection(cid)
            if prof is not None:
                resolved_groups.append((cid, eng, prof, grp))

        if not resolved_groups:
            if tolerate_conn_errors:
                state = self.get_workspace_state()
                state["warning"] = "尚未绑定贯标目标数据源连接，跳过自动实时核验"
                return state
            raise ValueError("尚未绑定贯标目标数据源连接，请先在顶部选择连接档案")

        start_pct = max(12.0, min(85.0, float(progress_start_pct)))
        all_targets = list(
            dict.fromkeys(
                (str(it.get("connection_id") or ""), str(it["schema_name"]), str(it["table_name"]))
                for it in items
            )
        )
        total_tables = max(1, len(all_targets))
        report(
            start_pct,
            "核验目标库状态",
            f"准备核验全局队列 {len(items)} 条规则（涉及 {len(resolved_groups)} 个数据源 / {total_tables} 张表）...",
            0,
            total_tables,
        )

        done_items = 0
        total_items = len(items)
        verify_warnings: List[str] = []
        for _cid, eng, prof, conn_items in resolved_groups:
            grp_start = start_pct + (92.0 - start_pct) * (done_items / max(1, total_items))
            grp_end = start_pct + (92.0 - start_pct) * (
                (done_items + len(conn_items)) / max(1, total_items)
            )
            conn_targets = list(
                dict.fromkeys((str(it["schema_name"]), str(it["table_name"])) for it in conn_items)
            )
            try:
                if prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host and prof.driver_track == "NATIVE":
                    mid_pct = grp_start + (grp_end - grp_start) * 0.35
                    report(
                        mid_pct,
                        "核验目标库状态",
                        f"正在从 [{prof.name}] 批量拉取 {len(conn_targets)} 张表系统目录与列序...",
                        done_items,
                        total_items,
                    )
                    eng.sync_all_items_with_db(items_subset=conn_items, dialect=prof.db_type)
                    done_items += len(conn_items)
                else:
                    # 跨任意驱动（含 SQLite / MySQL / 自定义 JDBC）单会话批量获取元数据并核验
                    probe_start = grp_start + 1.0
                    probe_end = grp_start + (grp_end - grp_start) * 0.68
                    meta_cache, _, _ = self.ctx.metadata_hub.get_tables_metadata_batch(
                        prof.connection_id,
                        conn_targets,
                        force_refresh=True,
                        on_progress=lambda done_c, tot_c, cs, ct, ps=probe_start, pe=probe_end, pname=prof.name: report(
                            ps + (pe - ps) * (min(done_c, max(1, tot_c)) / max(1, tot_c)),
                            "核验目标库状态",
                            f"正在读取 [{pname}] 表结构 ({min(done_c + 1, tot_c)}/{tot_c}): {cs}.{ct}",
                            done_c,
                            tot_c,
                        ),
                    )
                    def_sch = prof.effective_default_schema().lower()
                    tbl_items_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
                    tbl_meta_map: Dict[Tuple[str, str], EngineTableMeta] = {}
                    for it in conn_items:
                        key = (str(it["schema_name"]).lower(), str(it["table_name"]).lower())
                        tbl_items_map.setdefault(key, []).append(it)
                        if key not in tbl_meta_map:
                            raw_meta = meta_cache.get(key) or meta_cache.get((def_sch, key[1]))
                            if raw_meta is None:
                                raw_meta = self.ctx.metadata_hub.get_table_metadata(
                                    prof.connection_id,
                                    schema_name=it["schema_name"],
                                    table_name=it["table_name"],
                                    force_refresh=False,
                                )
                                meta_cache[key] = raw_meta
                            tbl_meta_map[key] = self._to_engine_table_meta(raw_meta)

                    updates: List[Dict[str, Any]] = []
                    for key, grp in tbl_items_map.items():
                        eng_meta = tbl_meta_map[key]
                        claimed_old_names = {
                            clean_identifier_input(str(x.get("old_col_name") or "")).lower()
                            for x in grp
                            if x.get("status") != "COMPLETED"
                        }
                        for it in grp:
                            upd = eng._build_single_item_sync_update(
                                it,
                                eng_meta,
                                claimed_old_names=claimed_old_names,
                                dialect=prof.db_type,
                            )
                            updates.append(upd)
                            done_items += 1
                            if done_items == 1 or done_items % 100 == 0 or done_items == total_items:
                                pct = probe_end + (grp_end - probe_end) * (
                                    done_items / max(1, total_items)
                                )
                                report(
                                    pct,
                                    "同步核验结果",
                                    f"正在比对字段规格 ({done_items}/{total_items}): {it['schema_name']}.{it['table_name']}.{it['old_col_name']}",
                                    done_items,
                                    total_items,
                                )
                    self.ctx.store.batch_update_items_sync(updates)
            except Exception as exc:
                if not tolerate_conn_errors:
                    raise
                done_items += len(conn_items)
                verify_warnings.append(f"[{prof.name}] 实时核验未完成: {exc}")

        report(
            95.0,
            "同步核验结果",
            f"已完成 {len(items)} 条字段规则的库内元数据实时比对",
            total_items,
            total_items,
        )
        state = self.get_workspace_state()
        if verify_warnings:
            state["warning"] = "；".join(verify_warnings)
        state["message"] = f"已完成 {len(items)} 条字段规则的库内元数据实时比对核验"
        return state

    def upload_excel_checklist(
        self,
        filename: str,
        content: bytes,
        default_schema: str = "",
        default_table: str = "",
        *,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        report(16.0, "校验与解析贯标清单", "正在读取工作簿并提取公式最终值...", 0, 0)
        validate_xlsx_bytes(filename, content)
        prof = self._sync_engine_with_active_profile()
        eff_schema = (
            (default_schema or "").strip()
            or (prof.effective_default_schema() if prof else "")
            or "public"
        )
        parsed_items = parse_excel_checklist(
            content,
            filename,
            default_schema=eff_schema,
            default_table=(default_table or "").strip(),
            row_progress_cb=lambda done_r, tot_r: report(
                min(54.0, 18.0 + 34.0 * done_r / max(1, tot_r)),
                "解析贯标清单与公式结果",
                f"正在读取工作表并计算公式结果（已处理 {done_r} 行）...",
                done_r,
                max(done_r, tot_r),
            ),
        )
        report(
            58.0,
            "保存贯标清单",
            f"已解析 {len(parsed_items)} 条改造字段规则，正在写入本地工作台...",
            len(parsed_items),
            len(parsed_items),
        )
        self.ctx.store.replace_all_items(parsed_items, filename, preserve_history=True)
        self.ctx.store.replace_cross_standards(
            parsed_items, filename, preserve_bindings=True
        )

        sync_warning = ""
        if prof:
            report(78.0, "核验目标库状态", f"正在与数据源 [{prof.name}] 比对字段现状...", len(parsed_items), len(parsed_items))
            try:
                self.sync_items(progress_cb=report, progress_start_pct=78.0)
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
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        if connection_id:
            self.bind_connection(
                connection_id,
                auto_sync=False,
                progress_cb=lambda pct, stg, dtl, p=0, t=0: report(
                    min(18.0, float(pct)), stg, dtl, p, t
                ),
            )
        prof = self._sync_engine_with_active_profile()
        if not prof:
            raise ValueError("请先选择有效的目标数据源连接")

        report(
            22.0,
            "扫描目标库表",
            f"正在连接 [{prof.name}] 并检索目标物理表（上限 {max_tables} 张）...",
            0,
            1,
        )
        if prof.db_type in ("DWS", "PGSQL", "KINGBASE") and prof.host and prof.driver_track == "NATIVE":
            report(
                45.0,
                "读取表字段结构",
                f"正在批量拉取 {schema_name or '全部模式'}.{table_name or '*'} 列定义与注释...",
                0,
                1,
            )
            cnt = self.engine.load_tables_from_database(
                schema_name=schema_name,
                table_name=table_name,
                max_tables=max_tables,
            )
            report(
                94.0,
                "同步执行台队列",
                f"已读取 {cnt} 个表字段结构至贯标队列",
                cnt,
                cnt,
            )
        else:
            tree = self.ctx.metadata_hub.refresh_catalog(
                prof.connection_id,
                schema_name=schema_name or None,
                progress_cb=lambda pct, stg, dtl, p=0, t=0: report(
                    22.0 + (min(100.0, max(0.0, float(pct))) / 100.0) * 26.0,
                    stg or "扫描目标库表",
                    dtl,
                    p,
                    t,
                ),
            )
            matched_tables = [
                t
                for t in tree["tables"]
                if t.get("object_type", "TABLE") == "TABLE"
                and (not schema_name or t["schema_name"].lower() == schema_name.strip().lower())
                and (not table_name or t["table_name"].lower() == table_name.strip().lower())
            ][:max_tables]
            loaded_items: List[Dict[str, Any]] = []
            tot_tbl = len(matched_tables)
            for idx, t in enumerate(matched_tables, start=1):
                pct = 48.0 + 34.0 * (idx / max(1, tot_tbl))
                report(
                    pct,
                    "读取表字段结构",
                    f"[{idx}/{tot_tbl}] 正在读取 {t['schema_name']}.{t['table_name']} 字段结构...",
                    idx,
                    tot_tbl,
                )
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
                report(
                    84.0,
                    "保存执行台队列",
                    f"正在写入 {cnt} 个字段结构并初始化队列...",
                    cnt,
                    cnt,
                )
                self.ctx.store.replace_all_items(loaded_items, label, preserve_history=False)
                self.sync_items(progress_cb=report, progress_start_pct=86.0)

        state = self.get_workspace_state()
        state["loaded_count"] = cnt
        state["message"] = (
            f"已从 [{prof.name}] 载入 {cnt} 个表字段结构至执行台"
            if cnt > 0
            else "未在目标数据源下匹配到符合条件的物理表"
        )
        return state

    def _post_migration_sync_bindings_and_metadata(
        self, completed_items: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """DDL 贯标执行成功后，同步迁移字段绑定、刷新表元数据及实时落标状态，杜绝改名后统计出现 6/8 等僵尸绑标."""
        grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for it in completed_items or []:
            if str(it.get("status") or "").upper() != "COMPLETED":
                continue
            cid = self._resolve_item_connection_id(it)
            sch = clean_identifier_input(str(it.get("schema_name") or "")) or "public"
            tbl = clean_identifier_input(str(it.get("table_name") or ""))
            if not cid or not tbl:
                continue
            gk = (cid, sch.lower(), tbl.lower())
            grp = grouped.setdefault(
                gk, {"connection_id": cid, "schema_name": sch, "table_name": tbl, "renames": []}
            )
            old_c = clean_identifier_input(str(it.get("old_col_name") or ""))
            new_c = clean_identifier_input(str(it.get("new_col_name") or ""))
            if old_c and new_c and old_c.lower() != new_c.lower():
                grp["renames"].append((old_c, new_c))

        migrated_tables: List[Dict[str, str]] = []
        touched_cids: List[str] = []
        for grp in grouped.values():
            cid = str(grp["connection_id"])
            sch = str(grp["schema_name"])
            tbl = str(grp["table_name"])
            if cid not in touched_cids:
                touched_cids.append(cid)
            migrated_tables.append(
                {"connection_id": cid, "schema_name": sch, "table_name": tbl}
            )
            if grp["renames"]:
                try:
                    self.ctx.standard_catalog.migrate_renamed_column_bindings(
                        cid, sch, tbl, grp["renames"]
                    )
                except Exception:
                    pass
            try:
                self.get_table_compliance(
                    cid,
                    sch,
                    tbl,
                    force_refresh=True,
                    include_table_stats=False,
                    purge_errors=False,
                )
            except Exception:
                pass

        table_stats_by_conn = {
            cid: self.ctx.standard_catalog.get_connection_binding_stats(cid)
            for cid in touched_cids
            if cid
        }
        return {
            "table_stats_by_conn": table_stats_by_conn,
            "migrated_tables": migrated_tables,
        }

    @staticmethod
    def _attach_post_migration_sync_state(
        state: Dict[str, Any], sync_meta: Dict[str, Any], fallback_cid: str = ""
    ) -> None:
        stats_by_conn = sync_meta.get("table_stats_by_conn") or {}
        if stats_by_conn:
            state["table_stats_by_conn"] = stats_by_conn
            active_cid = str(state.get("active_connection_id") or fallback_cid or "")
            if active_cid in stats_by_conn:
                state["table_stats"] = stats_by_conn[active_cid]
                state["table_stats_connection_id"] = active_cid
        if sync_meta.get("migrated_tables"):
            state["migrated_tables"] = sync_meta["migrated_tables"]

    def _auto_verify_after_execution(
        self,
        report: ProgressCallback,
        *,
        progress_start_pct: float = 90.0,
    ) -> Tuple[bool, str, List[int]]:
        """执行贯标 DDL 后立刻触发实时核验，并返回已确认达标的任务 ID 列表."""
        report(
            progress_start_pct,
            "实时核验目标库状态",
            "贯标执行完毕，正在对目标库最新字段结构执行实时核验...",
            0,
            1,
        )
        try:
            verify_state = self.sync_items(
                progress_cb=lambda pct, stg, dtl, proc=0, tot=0: report(
                    min(98.5, max(progress_start_pct, float(pct))),
                    stg or "实时核验目标库状态",
                    dtl,
                    proc,
                    tot,
                ),
                progress_start_pct=progress_start_pct,
                tolerate_conn_errors=True,
            )
            verified_items = verify_state.get("items") or self.ctx.store.list_items()
            verified_completed_ids = [
                int(it["id"])
                for it in verified_items
                if str(it.get("status") or "").upper() == "COMPLETED"
                and int(it.get("id") or 0) > 0
            ]
            verify_warn = str(verify_state.get("warning") or "").strip()
            if verify_warn:
                return False, verify_warn, verified_completed_ids
            return True, "", verified_completed_ids
        except Exception as exc:
            return False, f"执行后自动实时核验未完全完成：{exc}", []

    def execute_single(
        self,
        item_id: int,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        report(20.0, "准备执行贯标", f"正在加载字段任务 #{item_id} 与目标库连接...", 0, 1)
        item_before = self.ctx.store.get_item(item_id)
        conn_id = self._resolve_item_connection_id(item_before) if item_before else ""
        eng, prof = self._get_engine_for_connection(conn_id)
        if prof is not None and not (
            prof.db_type in ("DWS", "PGSQL", "KINGBASE")
            and prof.host
            and prof.driver_track == "NATIVE"
        ):
            raise ValueError(
                f"数据源 [{prof.name}]（{prof.db_type}）暂不支持在线直连执行贯标 DDL，请点击「SQL」复制标准语句后在目标库执行"
            )
        item_label = (
            f"{item_before['schema_name']}.{item_before['table_name']}.{item_before['old_col_name']}"
            if item_before
            else f"任务 #{item_id}"
        )
        report(
            48.0,
            "执行原生 ALTER",
            f"正在对 {item_label} 执行字段贯标与列序校验...",
            0,
            1,
        )
        item = eng.execute_single_item_direct(item_id)
        sync_meta: Dict[str, Any] = {"table_stats_by_conn": {}, "migrated_tables": []}
        if item.get("status") == "COMPLETED":
            report(
                82.0,
                "同步字段绑标与元数据",
                f"正在同步 {item_label} 改名后绑定关系与落标统计...",
                1,
                1,
            )
            sync_meta = self._post_migration_sync_bindings_and_metadata([item])

        auto_verified, verify_warn, verified_completed_ids = (
            self._auto_verify_after_execution(report, progress_start_pct=88.0)
        )
        item = self.ctx.store.get_item(item_id) or item
        report(
            98.5,
            "刷新执行台状态",
            f"{item_label} 状态: {item.get('status', 'DONE')}",
            1,
            1,
        )
        state = self.get_workspace_state()
        state["item"] = item
        state["auto_verified"] = auto_verified
        state["verified_completed_ids"] = verified_completed_ids
        self._attach_post_migration_sync_state(state, sync_meta, fallback_cid=conn_id)
        if item.get("status") in ("FAILED", "MISSING_TABLE", "MISSING_COLUMN"):
            state["warning"] = (
                item.get("error_message") or item.get("status_detail") or f"{item_label} 贯标执行未通过"
            )
        elif verify_warn:
            state["warning"] = verify_warn
            state["message"] = item.get("status_detail") or f"字段 {item_label} 贯标执行完成"
        else:
            state["message"] = item.get("status_detail") or f"字段 {item_label} 贯标执行完成"
        return state

    def execute_batch(
        self,
        item_ids: List[int],
        max_workers: int = 5,
        keep_backup: bool = True,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        self._sync_engine_with_active_profile()

        id_set = {int(x) for x in (item_ids or [])}
        initial_items = self.ctx.store.list_items()
        pending_items = [
            it for it in initial_items if it["id"] in id_set and it["status"] != "COMPLETED"
        ]
        if not pending_items:
            report(82.0, "调度并发贯标", "所选记录均已贯标完成，正在触发实时核验...", 0, 0)
            batch_res = self.engine.execute_batch_items(
                item_ids=item_ids, max_workers=max_workers, keep_backup=keep_backup
            )
            auto_verified, verify_warn, verified_completed_ids = (
                self._auto_verify_after_execution(report, progress_start_pct=86.0)
            )
            state = self.get_workspace_state()
            state["batch_result"] = batch_res
            state["auto_verified"] = auto_verified
            state["verified_completed_ids"] = verified_completed_ids
            state["message"] = batch_res.get("message", "批量贯标任务执行完毕")
            if verify_warn:
                state["warning"] = verify_warn
            return state

        conn_pending_groups: Dict[str, List[Dict[str, Any]]] = {}
        for it in pending_items:
            cid = self._resolve_item_connection_id(it)
            conn_pending_groups.setdefault(cid, []).append(it)

        conn_engines: Dict[str, DWSMigratorEngine] = {}
        for cid in conn_pending_groups:
            eng, prof = self._get_engine_for_connection(cid)
            if prof is not None and not (
                prof.db_type in ("DWS", "PGSQL", "KINGBASE")
                and prof.host
                and prof.driver_track == "NATIVE"
            ):
                raise ValueError(
                    f"数据源 [{prof.name}]（{prof.db_type}）暂不支持在线直连批量执行，请取消勾选该数据源任务或点击「SQL」复制语句手工执行"
                )
            conn_engines[cid] = eng

        table_groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        table_labels: Dict[Tuple[str, str, str], str] = {}
        orig_item_state: Dict[int, Tuple[str, str, str, str, str]] = {}
        for it in pending_items:
            cid = self._resolve_item_connection_id(it)
            sch_clean = clean_identifier_input(it["schema_name"]) or str(it["schema_name"])
            tbl_clean = clean_identifier_input(it["table_name"]) or str(it["table_name"])
            k = (cid, sch_clean.lower(), tbl_clean.lower())
            table_groups.setdefault(k, []).append(it)
            table_labels.setdefault(k, f"{sch_clean}.{tbl_clean}")
            orig_item_state[it["id"]] = (
                str(it.get("status") or ""),
                str(it.get("executed_at") or ""),
                str(it.get("updated_at") or ""),
                str(it.get("status_detail") or ""),
                str(it.get("error_message") or ""),
            )

        total_tables = len(table_groups)
        worker_count = max(1, min(int(max_workers or 5), total_tables))
        report(
            18.0,
            "调度并发贯标",
            f"共 {len(pending_items)} 个字段（{total_tables} 张表），启动 {worker_count} 线程按表并发执行...",
            0,
            total_tables,
        )

        reported_tables: Set[Tuple[str, str, str]] = set()
        report_lock = threading.Lock()
        stop_evt = threading.Event()

        def _scan_finished_tables(*, force_all: bool = False) -> None:
            if report is noop_progress:
                return
            try:
                cur_by_id = {it["id"]: it for it in self.ctx.store.list_items()}
            except Exception:
                return
            with report_lock:
                for t_key, grp in table_groups.items():
                    if t_key in reported_tables:
                        continue
                    cur_grp = [cur_by_id.get(it["id"]) for it in grp]
                    if any(c is None for c in cur_grp):
                        continue
                    all_changed = all(
                        (
                            str(c.get("status") or ""),
                            str(c.get("executed_at") or ""),
                            str(c.get("updated_at") or ""),
                            str(c.get("status_detail") or ""),
                            str(c.get("error_message") or ""),
                        )
                        != orig_item_state[c["id"]]
                        for c in cur_grp
                        if c is not None
                    )
                    if all_changed or force_all:
                        reported_tables.add(t_key)
                        done_n = len(reported_tables)
                        ok_c = sum(1 for c in cur_grp if c and c.get("status") == "COMPLETED")
                        err_c = sum(
                            1
                            for c in cur_grp
                            if c
                            and c.get("status")
                            in ("FAILED", "MISSING_TABLE", "MISSING_COLUMN")
                        )
                        pct = 20.0 + 72.0 * (done_n / max(1, total_tables))
                        tbl_lbl = table_labels[t_key]
                        if all_changed:
                            detail = f"[{done_n}/{total_tables}] 表 {tbl_lbl} 执行完成（成功 {ok_c} / 异常 {err_c}）"
                        else:
                            detail = f"[{done_n}/{total_tables}] 表 {tbl_lbl} 已处理"
                        report(pct, "并发执行贯标", detail, done_n, total_tables)

        watcher: Optional[threading.Thread] = None
        if report is not noop_progress:
            def _watch_loop() -> None:
                while not stop_evt.wait(0.1):
                    _scan_finished_tables(force_all=False)

            watcher = threading.Thread(
                target=_watch_loop,
                name="MigratorBatchProgressWatcher",
                daemon=True,
            )
            watcher.start()

        try:
            if len(conn_pending_groups) == 1:
                only_cid = next(iter(conn_pending_groups.keys()))
                batch_res = conn_engines[only_cid].execute_batch_items(
                    item_ids=item_ids, max_workers=max_workers, keep_backup=keep_backup
                )
            else:
                agg_executed = 0
                agg_success = 0
                agg_failed = 0
                agg_rebuild_tables = 0
                agg_direct_tables = 0
                for cid, grp_items in conn_pending_groups.items():
                    sub_res = conn_engines[cid].execute_batch_items(
                        item_ids=[it["id"] for it in grp_items],
                        max_workers=max_workers,
                        keep_backup=keep_backup,
                    )
                    agg_executed += int(sub_res.get("executed_count") or 0)
                    agg_success += int(sub_res.get("success_count") or 0)
                    agg_failed += int(sub_res.get("failed_count") or 0)
                    agg_rebuild_tables += int(sub_res.get("rebuild_table_count") or 0)
                    agg_direct_tables += int(sub_res.get("direct_table_count") or 0)
                batch_res = {
                    "executed_count": agg_executed,
                    "success_count": agg_success,
                    "failed_count": agg_failed,
                    "rebuild_table_count": agg_rebuild_tables,
                    "direct_table_count": agg_direct_tables,
                    "message": (
                        f"批量并发执行完毕（跨 {len(conn_pending_groups)} 个数据源）：共处理 {total_tables} 张表"
                        f"（原序重建 {agg_rebuild_tables} 张、原生 ALTER {agg_direct_tables} 张），"
                        f"成功 {agg_success} 个字段，失败 {agg_failed} 个字段。"
                    ),
                }
        finally:
            stop_evt.set()
            if watcher is not None:
                watcher.join(timeout=1.5)

        _scan_finished_tables(force_all=True)
        pending_id_set = {int(it["id"]) for it in pending_items}
        latest_completed = [
            it
            for it in self.ctx.store.list_items()
            if it["id"] in pending_id_set and it.get("status") == "COMPLETED"
        ]
        sync_meta: Dict[str, Any] = {"table_stats_by_conn": {}, "migrated_tables": []}
        if latest_completed:
            report(
                88.0,
                "同步字段绑标与元数据",
                "正在同步已完成表的新列绑定关系与落标统计...",
                total_tables,
                total_tables,
            )
            sync_meta = self._post_migration_sync_bindings_and_metadata(latest_completed)

        auto_verified, verify_warn, verified_completed_ids = (
            self._auto_verify_after_execution(report, progress_start_pct=91.0)
        )
        report(
            98.5,
            "汇总批量结果",
            batch_res.get("message", "批量贯标任务执行完毕"),
            total_tables,
            total_tables,
        )

        state = self.get_workspace_state()
        state["batch_result"] = batch_res
        state["auto_verified"] = auto_verified
        state["verified_completed_ids"] = verified_completed_ids
        state["message"] = batch_res.get("message", "批量贯标任务执行完毕")
        self._attach_post_migration_sync_state(state, sync_meta)
        if int(batch_res.get("failed_count", 0) or 0) > 0:
            state["warning"] = state["message"]
        elif verify_warn:
            state["warning"] = verify_warn
        return state

    def preview_rebuild(
        self,
        schema_name: str,
        table_name: str,
        item_ids: Optional[List[int]] = None,
        keep_backup: bool = True,
        connection_id: str = "",
    ) -> Dict[str, Any]:
        eff_cid = (connection_id or "").strip()
        all_items = self.ctx.store.list_items()
        if item_ids:
            id_set = {int(x) for x in item_ids}
            tbl_items = [it for it in all_items if it["id"] in id_set]
            if not eff_cid and tbl_items:
                eff_cid = self._resolve_item_connection_id(tbl_items[0])
        else:
            tbl_items = [
                it
                for it in all_items
                if it["schema_name"].lower() == schema_name.lower()
                and it["table_name"].lower() == table_name.lower()
                and it["status"] != "COMPLETED"
                and (not eff_cid or self._resolve_item_connection_id(it) == eff_cid)
            ]
            if not eff_cid and tbl_items:
                eff_cid = self._resolve_item_connection_id(tbl_items[0])

        eng, prof = self._get_engine_for_connection(eff_cid)

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

        plan = eng.build_table_rebuild_sql_preview(
            schema_name,
            table_name,
            tbl_items,
            keep_backup=keep_backup,
            table_meta_override=meta_override,
        )
        t_meta_info = plan.get("table_meta") or {}
        return {
            "ok": True,
            "connection_id": prof.connection_id if prof else eff_cid,
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
        connection_id: str = "",
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        eff_cid = (connection_id or "").strip()
        all_items = self.ctx.store.list_items()
        if item_ids is not None:
            id_set = {int(x) for x in item_ids}
            tbl_items = [it for it in all_items if it["id"] in id_set]
            if not eff_cid and tbl_items:
                eff_cid = self._resolve_item_connection_id(tbl_items[0])
            scoped_item_ids: Optional[List[int]] = item_ids
        else:
            tbl_items = [
                it
                for it in all_items
                if it["schema_name"].lower() == schema_name.lower()
                and it["table_name"].lower() == table_name.lower()
                and it["status"] != "COMPLETED"
                and (not eff_cid or self._resolve_item_connection_id(it) == eff_cid)
            ]
            if not eff_cid and tbl_items:
                eff_cid = self._resolve_item_connection_id(tbl_items[0])
            scoped_item_ids = [it["id"] for it in tbl_items] or None

        eng, prof = self._get_engine_for_connection(eff_cid)
        if prof is not None and not (
            prof.db_type in ("DWS", "PGSQL", "KINGBASE")
            and prof.host
            and prof.driver_track == "NATIVE"
        ):
            raise ValueError(
                f"数据源 [{prof.name}]（{prof.db_type}）暂不支持在线直连保序重建，请复制 SQL 手工执行"
            )

        tbl_label = f"{schema_name}.{table_name}"
        report(
            20.0,
            "分析列序与依赖",
            f"正在分析 {tbl_label} 物理列序、分布/分区键及改名拓扑依赖...",
            0,
            3,
        )

        baseline_logs = self.ctx.store.list_logs(limit=1)
        baseline_log_id = int(baseline_logs[0]["id"]) if baseline_logs else 0
        stage_lock = threading.Lock()
        stage_state = {"built_tmp": False}
        stop_evt = threading.Event()

        def _watch_rebuild_stage() -> None:
            while not stop_evt.wait(0.1):
                try:
                    recent = self.ctx.store.list_logs(limit=10)
                except Exception:
                    continue
                for lg in recent:
                    if int(lg.get("id") or 0) <= baseline_log_id:
                        continue
                    msg = str(lg.get("message") or "")
                    if "开始执行 [按原表字段顺序重建表迁移]" in msg:
                        with stage_lock:
                            if not stage_state["built_tmp"]:
                                stage_state["built_tmp"] = True
                                report(
                                    52.0,
                                    "构建保序临时表",
                                    f"正在构建 {tbl_label} 保序临时表并按原列序迁移数据...",
                                    1,
                                    3,
                                )
                        return

        watcher: Optional[threading.Thread] = None
        if report is not noop_progress:
            watcher = threading.Thread(
                target=_watch_rebuild_stage,
                name="MigratorRebuildProgressWatcher",
                daemon=True,
            )
            watcher.start()

        try:
            res = eng.execute_table_rebuild_migration(
                schema_name, table_name, item_ids=scoped_item_ids, keep_backup=keep_backup
            )
        finally:
            stop_evt.set()
            if watcher is not None:
                watcher.join(timeout=1.5)

        with stage_lock:
            if not stage_state["built_tmp"]:
                stage_state["built_tmp"] = True
                report(
                    52.0,
                    "构建保序临时表",
                    f"已构建 {tbl_label} 保序临时表并完成数据迁移",
                    1,
                    3,
                )

        report(
            85.0,
            "切换表名与恢复索引",
            f"正在切换 {tbl_label} 表名、同步字段绑定与结构校验...",
            2,
            3,
        )
        tbl_id_set = {int(it["id"]) for it in tbl_items}
        latest_completed = [
            it
            for it in self.ctx.store.list_items()
            if it.get("status") == "COMPLETED"
            and (
                (tbl_id_set and it["id"] in tbl_id_set)
                or (
                    not tbl_id_set
                    and it["schema_name"].lower() == schema_name.lower()
                    and it["table_name"].lower() == table_name.lower()
                    and (not eff_cid or self._resolve_item_connection_id(it) == eff_cid)
                )
            )
        ]
        sync_meta = self._post_migration_sync_bindings_and_metadata(latest_completed)
        auto_verified, verify_warn, verified_completed_ids = (
            self._auto_verify_after_execution(report, progress_start_pct=90.0)
        )
        state = self.get_workspace_state()
        state["rebuild_result"] = res
        state["auto_verified"] = auto_verified
        state["verified_completed_ids"] = verified_completed_ids
        state["message"] = res.get("message", "整表保序重建完成")
        self._attach_post_migration_sync_state(state, sync_meta, fallback_cid=eff_cid)
        if verify_warn:
            state["warning"] = verify_warn
        report(
            98.5,
            "切换表名与实时核验",
            state["message"],
            3,
            3,
        )
        return state

    def clear_items(self) -> Dict[str, Any]:
        self.ctx.store.clear_items()
        return self.get_workspace_state()

    def delete_items(self, item_ids: List[int]) -> Dict[str, Any]:
        deleted = self.ctx.store.delete_migration_items(item_ids)
        state = self.get_workspace_state()
        state["deleted_count"] = deleted
        state["message"] = f"已从贯标队列移除 {deleted} 条任务"
        return state

    def preview_item_sql(self, item_id: int) -> Dict[str, Any]:
        item = self.ctx.store.get_item(item_id)
        if not item:
            raise ValueError("未找到指定贯标任务")
        conn_id = self._resolve_item_connection_id(item)
        eng, prof = self._get_engine_for_connection(conn_id)
        dialect = prof.db_type if prof else "DWS"

        eng_meta: Optional[EngineTableMeta] = None
        if prof is not None:
            t_meta = self.ctx.metadata_hub.get_table_metadata(
                prof.connection_id,
                schema_name=item["schema_name"],
                table_name=item["table_name"],
                force_refresh=False,
            )
            if t_meta and t_meta.exists:
                eng_meta = self._to_engine_table_meta(t_meta)

        matched_col: Optional[EngineColumnMeta] = None
        if eng_meta is not None:
            col_by_lower = {
                clean_identifier_input(c.column_name).lower(): c for c in eng_meta.columns
            }
            for cand_raw in (
                item.get("db_current_col_name") or item.get("current_db_col_name"),
                item.get("old_col_name"),
                item.get("new_col_name"),
            ):
                c_clean = clean_identifier_input(str(cand_raw or "")).lower()
                if c_clean and c_clean in col_by_lower:
                    matched_col = col_by_lower[c_clean]
                    break

        changes = analyze_item_changes(item, matched_col=matched_col, table_meta=eng_meta)
        needs_rebuild = item_requires_table_rebuild(
            item, matched_col=matched_col, table_meta=eng_meta
        )
        sql_preview = eng.build_direct_alter_sql_preview(
            item, matched_col=matched_col, table_meta=eng_meta, dialect=dialect
        )
        can_execute_online = bool(
            prof
            and prof.db_type in ("DWS", "PGSQL", "KINGBASE")
            and prof.host
            and prof.driver_track == "NATIVE"
        )
        strategy = "TABLE_REBUILD" if needs_rebuild else "DIRECT_ALTER"
        return {
            "ok": True,
            "item_id": item["id"],
            "connection_id": prof.connection_id if prof else conn_id,
            "connection_name": prof.name if prof else "",
            "dialect": dialect,
            "schema_name": item["schema_name"],
            "table_name": item["table_name"],
            "old_col_name": item["old_col_name"],
            "new_col_name": item["new_col_name"],
            "strategy": strategy,
            "needs_rebuild": needs_rebuild,
            "is_comment_only": bool(changes["is_comment_only"]),
            "has_any_change": bool(changes["has_any_change"]),
            "changes": changes,
            "sql_preview": sql_preview,
            "can_execute_online": can_execute_online,
        }

    @staticmethod
    def generate_template_bytes() -> bytes:
        return generate_template_excel()

    def export_report_bytes(self) -> bytes:
        return export_report_excel(self.ctx.store.list_items())
