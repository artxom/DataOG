"""跨方言建表 DDL 翻译与四层视图血缘穿透服务 (DdlTranslatorService).

严格遵循插件隔离规范：仅依赖 `core.platform`，不与任何其他业务模块私有互引。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from core.platform.context import PlatformContext
from core.platform.dialects import (
    build_dialect_ddl,
    clean_identifier_input,
    enrich_view_columns_by_lineage,
    format_readable_sql_ddl,
    parse_view_sql_lineage,
)
from core.platform.models import ColumnMeta, TableMeta


class DdlTranslatorService:
    """支持任意连接表/视图、数据标准清单及离线/在线四层穿透视图 SQL 的多方言建表翻译引擎."""

    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def _resolve_connection_id(self, mode: str, explicit_conn_id: str) -> str:
        if explicit_conn_id and explicit_conn_id.strip():
            return explicit_conn_id.strip()
        m_low = (mode or "").strip().lower()
        if m_low == "source":
            return str(self.ctx.store.get_meta("cross_source_connection_id", "") or "")
        if m_low == "target":
            return str(
                self.ctx.store.get_meta("cross_target_connection_id", "")
                or self.ctx.store.get_meta("migrator_connection_id", "")
                or ""
            )
        # 默认回退到最近一个有效连接档案
        profiles = self.ctx.connection_hub.list_profiles()
        return profiles[0]["connection_id"] if profiles else ""

    def _build_meta_from_standard(
        self, std_table_key: str, fallback_schema: str, fallback_table: str
    ) -> TableMeta:
        t_key = clean_identifier_input(std_table_key or fallback_table).lower()
        tb_bind = self.ctx.store.get_cross_table_binding(t_key) if t_key else None
        col_binds = self.ctx.store.list_cross_columns(t_key) if t_key else []

        if col_binds:
            cols = [
                ColumnMeta(
                    ordinal_position=idx,
                    column_name=str(cb.get("std_col_name") or cb.get("orig_col_name") or f"col_{idx}"),
                    data_type=str(cb.get("std_col_type") or cb.get("orig_col_type") or "VARCHAR(255)"),
                    column_comment=str(cb.get("std_col_comment") or cb.get("orig_col_comment") or ""),
                )
                for idx, cb in enumerate(col_binds, start=1)
            ]
            return TableMeta(
                schema_name=str((tb_bind or {}).get("std_schema_name") or fallback_schema or "public"),
                table_name=str((tb_bind or {}).get("std_table_name") or fallback_table or t_key),
                exists=True,
                table_comment=str((tb_bind or {}).get("std_table_comment") or ""),
                columns=cols,
                object_type="TABLE",
            )

        # 回退查找单库贯标清单中的目标表记录
        mig_items = [
            it
            for it in self.ctx.store.list_items()
            if (not t_key or str(it.get("table_name", "")).lower() == t_key)
        ]
        if mig_items:
            first_tbl = str(mig_items[0].get("table_name") or fallback_table or "std_table")
            same_tbl = [
                it for it in mig_items if str(it.get("table_name", "")).lower() == first_tbl.lower()
            ]
            cols = [
                ColumnMeta(
                    ordinal_position=idx,
                    column_name=str(it.get("new_col_name") or it.get("old_col_name") or f"col_{idx}"),
                    data_type=str(it.get("new_col_type") or it.get("old_col_type") or "VARCHAR(255)"),
                    column_comment=str(it.get("new_col_comment") or it.get("old_col_comment") or ""),
                )
                for idx, it in enumerate(same_tbl, start=1)
            ]
            return TableMeta(
                schema_name=str(same_tbl[0].get("schema_name") or fallback_schema or "public"),
                table_name=first_tbl,
                exists=True,
                columns=cols,
                object_type="TABLE",
            )

        raise ValueError("当前尚未导入数据标准清单或未找到目标标准表结构，请先导入数标清单或切换为从数据源连接读取")

    def _apply_standard_overlay(
        self, meta: TableMeta, std_table_key: str, mode: str
    ) -> TableMeta:
        t_key = clean_identifier_input(std_table_key or meta.table_name).lower()
        col_binds = self.ctx.store.list_cross_columns(t_key)
        if not col_binds:
            return meta

        out = copy.deepcopy(meta)
        tb_bind = self.ctx.store.get_cross_table_binding(t_key)
        if tb_bind and tb_bind.get("std_table_comment") and not out.table_comment:
            out.table_comment = str(tb_bind["std_table_comment"])

        by_src: Dict[str, Dict[str, Any]] = {}
        for cb in col_binds:
            for k in (
                cb.get("bound_source_col"),
                cb.get("orig_col_name"),
                cb.get("bound_target_col"),
                cb.get("std_col_name"),
            ):
                ck = clean_identifier_input(str(k or "")).lower()
                if ck and ck not in by_src:
                    by_src[ck] = cb

        rename_map: Dict[str, str] = {}
        for col in out.columns:
            cb = by_src.get(col.column_name.lower())
            if not cb:
                continue
            std_nm = clean_identifier_input(str(cb.get("std_col_name") or ""))
            std_tp = str(cb.get("std_col_type") or "").strip()
            std_cm = str(cb.get("std_col_comment") or "").strip()
            if std_nm and std_nm.lower() != col.column_name.lower():
                rename_map[col.column_name.lower()] = std_nm
                col.column_name = std_nm
            if std_tp:
                col.data_type = std_tp
            if std_cm:
                col.column_comment = std_cm

        if rename_map:
            out.distribute_keys = [
                rename_map.get(k.lower(), k) for k in (out.distribute_keys or [])
            ]
            out.partition_keys = [
                rename_map.get(k.lower(), k) for k in (out.partition_keys or [])
            ]
        return out

    def translate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        eff_mode = (
            str(payload.get("source_role") or payload.get("source_mode") or "connection")
            .strip()
            .lower()
        )
        source_schema = clean_identifier_input(str(payload.get("source_schema") or ""))
        source_table = clean_identifier_input(str(payload.get("source_table") or ""))
        target_dialect = str(payload.get("target_dialect") or "DWS").strip().upper()
        storage_mode = str(payload.get("storage_mode") or "AUTO").strip().upper()
        target_schema = clean_identifier_input(str(payload.get("target_schema") or ""))
        target_table = clean_identifier_input(str(payload.get("target_table") or ""))
        apply_std = bool(payload.get("apply_standard_mapping", False))
        std_table_key = str(payload.get("std_table_key") or "").strip()
        view_sql = str(payload.get("view_sql") or "").strip()
        force_refresh = bool(payload.get("force_refresh", False))

        from_live_db = False
        source_dialect = "SQL"
        source_ddl = ""

        # 模式 A：直接粘贴视图定义 CREATE VIEW / SELECT 语句（支持可选在线基表四层递归穿透）
        if eff_mode == "view_sql" or (view_sql and not source_table):
            if not view_sql:
                raise ValueError("请粘贴有效的 CREATE VIEW ... AS SELECT ... 或 SELECT 查询语句")
            parsed_v = parse_view_sql_lineage(
                view_sql, default_schema=source_schema or "public"
            )
            if not parsed_v.get("is_view_sql") or not parsed_v.get("columns"):
                raise ValueError("未能从输入的 SQL 中解析出有效的 SELECT 投影列，请检查 SQL 语法")

            v_sch = parsed_v.get("view_schema") or source_schema or "public"
            v_tbl = parsed_v.get("view_name") or source_table or target_table or "v_translated_table"
            meta = TableMeta(
                schema_name=v_sch,
                table_name=v_tbl,
                exists=True,
                table_comment=parsed_v.get("view_comment") or "",
                columns=[
                    ColumnMeta(
                        ordinal_position=c["ordinal_position"],
                        column_name=c["column_name"],
                        data_type=c["inferred_type"],
                        column_comment=c.get("inline_comment", ""),
                    )
                    for c in parsed_v["columns"]
                ],
                raw_ddl=view_sql,
                object_type="VIEW",
                view_definition=view_sql,
            )

            probe_cid = (
                str(payload.get("probe_connection_id") or payload.get("connection_id") or "").strip()
                or str(self.ctx.store.get_meta("cross_source_connection_id", "") or "")
            )
            probe_fn = None
            if probe_cid and self.ctx.connection_hub.get_profile(probe_cid):
                prof = self.ctx.connection_hub.get_profile(probe_cid)
                source_dialect = prof.db_type if prof else "VIEW_SQL"
                from_live_db = True

                def _fetch_live_obj(up_s: str, up_t: str) -> Optional[TableMeta]:
                    return self.ctx.metadata_hub.get_table_metadata(
                        probe_cid, up_s, up_t, force_refresh=False, penetrate_views=True
                    )

                probe_fn = _fetch_live_obj
            else:
                source_dialect = "VIEW_SQL"

            meta = enrich_view_columns_by_lineage(
                meta, fetch_object_fn=probe_fn, max_depth=4
            )
            source_ddl = format_readable_sql_ddl(view_sql)

        # 模式 B：基于数据标准清单直接生成任意方言建表 DDL
        elif eff_mode == "standard":
            meta = self._build_meta_from_standard(
                std_table_key=std_table_key or source_table,
                fallback_schema=source_schema or "public",
                fallback_table=source_table or std_table_key,
            )
            source_dialect = "DATA_STANDARD"
            src_built = build_dialect_ddl(
                meta,
                source_dialect="DATA_STANDARD",
                target_dialect="PGSQL",
                target_schema=meta.schema_name,
                target_table=meta.table_name,
            )
            source_ddl = src_built["ddl"]

        # 模式 C：从任意命名数据源连接（含内置原生库与自定义 JDBC 库）读取表或视图
        else:
            conn_id = self._resolve_connection_id(
                eff_mode, str(payload.get("connection_id") or "")
            )
            if not conn_id:
                raise ValueError("请先选择有效的来源数据源连接，或切换为「粘贴视图 SQL」/「数据标准」模式")
            profile = self.ctx.connection_hub.require_profile(conn_id)
            if not source_table:
                raise ValueError("请输入或选择要读取转换的来源表名/视图名")
            meta = self.ctx.metadata_hub.get_table_metadata(
                conn_id,
                schema_name=source_schema or profile.default_schema,
                table_name=source_table,
                force_refresh=force_refresh,
                penetrate_views=True,
            )
            if not meta.exists or not meta.columns:
                raise ValueError(
                    f"在数据源 [{profile.name}] 中未找到表或视图 [{source_schema or profile.default_schema}.{source_table}]"
                )
            from_live_db = True
            source_dialect = profile.db_type
            if meta.raw_ddl:
                source_ddl = format_readable_sql_ddl(meta.raw_ddl)
            else:
                reconstructed = build_dialect_ddl(
                    meta,
                    source_dialect=profile.db_type,
                    target_dialect=profile.db_type,
                    target_schema=meta.schema_name,
                    target_table=meta.table_name,
                )
                source_ddl = reconstructed["ddl"]

        if apply_std:
            meta = self._apply_standard_overlay(
                meta, std_table_key=std_table_key or meta.table_name, mode=eff_mode
            )

        eff_target_table = target_table or meta.table_name
        if meta.object_type in ("VIEW", "MATVIEW") and not target_table:
            eff_target_table = meta.table_name

        built = build_dialect_ddl(
            meta,
            source_dialect=source_dialect,
            target_dialect=target_dialect,
            target_schema=target_schema or meta.schema_name,
            target_table=eff_target_table,
            storage_mode=storage_mode,
        )

        self.ctx.store.add_log(
            "INFO",
            "DDL翻译器",
            f"已完成 [{meta.schema_name}.{meta.table_name}] ({source_dialect} -> {built['target_dialect']}) 建表 DDL 转换，共 {built['column_count']} 列。",
            sql_text=built["ddl"][:2000],
        )

        return {
            "ok": True,
            "source_dialect": source_dialect,
            "source_schema": meta.schema_name,
            "source_table": meta.table_name,
            "source_object_type": meta.object_type or "TABLE",
            "penetrated_base_tables": list(meta.penetrated_base_tables or []),
            "from_live_db": from_live_db,
            "source_ddl": source_ddl,
            "target_dialect": built["target_dialect"],
            "target_schema": built["target_schema"],
            "target_table": built["target_table"],
            "storage_mode": built["storage_mode"],
            "column_count": built["column_count"],
            "target_ddl": built["ddl"],
            "column_mappings": built["column_mappings"],
        }
