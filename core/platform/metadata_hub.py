"""DataOG 全局持久化元数据共享中枢 (MetadataHub).

核心能力：
1. 双层探查引擎：
   - 深度定制高速探查器：`DWS` / `PGSQL`（系统目录单连接批量探查 + 分区边界与分布键提取）、`INCEPTOR`（`DESCRIBE FORMATTED` + `SHOW CREATE TABLE` + 零行编译期列结构探测）、`MYSQL`（`information_schema`）、`SQLITE`（`sqlite_master` + `PRAGMA`）；
   - 通用 JDBC 回退探查器：针对任意自定义 `.jar` 驱动数据库（达梦 DM8、Oracle、DB2、GBase 等），自动调用标准 `java.sql.DatabaseMetaData` 提取 Schema、表/视图、字段类型、注释与主键；
2. 四层递归视图血缘穿透 (`max_depth=4`)：自动逆向解析视图上游基表与级联视图，补全精确字段类型、中文注释与分布/分区键；
3. 本地 SQLite 元数据索引持久化缓存：采集一次、零延迟共享给全平台所有业务插件模块，支持增量刷新与秒级全局检索。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from core.platform.connection_hub import ConnectionHub
from core.platform.dialects import (
    SYSTEM_SCHEMAS,
    clean_identifier_input,
    enrich_view_columns_by_lineage,
    format_partition_boundary_val,
    format_reloptions_with_clause,
    normalize_cross_type,
    parse_dws_tabledef,
    parse_view_sql_lineage,
    partition_clause_has_child_defs,
    quote_backtick_str,
    quote_ident_str,
    quote_literal_str,
    validate_identifier,
)
from core.platform.driver_manager import JdbcConnectionAdapter
from core.platform.inceptor_client import InceptorThriftClient
from core.platform.models import ColumnMeta, ConnectionProfile, SchemaMeta, TableMeta
from core.platform.storage import PlatformStateStore

logger = logging.getLogger("dataog")


class MetadataHub:
    """跨数据源统一元数据探查、四层视图血缘补全与 SQLite 本地索引中心."""

    def __init__(self, store: PlatformStateStore, connection_hub: ConnectionHub) -> None:
        self.store = store
        self.connection_hub = connection_hub

    @staticmethod
    def _finalize_table_meta(meta: TableMeta) -> TableMeta:
        pk_lower = {k.lower() for k in (meta.primary_keys or [])}
        for col in meta.columns:
            if not col.normalized_type:
                col.normalized_type = normalize_cross_type(col.data_type)
            if col.column_name.lower() in pk_lower:
                col.is_primary_key = True
        return meta

    # =========================================================================
    # 对外公共查询、增量刷新与全文检索 API
    # =========================================================================

    def get_catalog_tree(
        self, connection_id: str, auto_refresh: bool = False
    ) -> Dict[str, Any]:
        cached = self.store.get_cached_catalog(connection_id)
        if auto_refresh and not cached["schemas"] and not cached["tables"]:
            return self.refresh_catalog(connection_id)
        profile = self.connection_hub.get_profile(connection_id)
        cached["default_schema"] = profile.default_schema if profile else "public"
        cached["db_type"] = profile.db_type if profile else ""
        return cached

    def refresh_catalog(
        self, connection_id: str, schema_name: Optional[str] = None
    ) -> Dict[str, Any]:
        profile = self.connection_hub.require_profile(connection_id)
        clean_schema = clean_identifier_input(schema_name or "") or None

        with self.connection_hub.session(connection_id, timeout_sec=15) as conn:
            if isinstance(conn, InceptorThriftClient):
                schemas, tables = self._probe_inceptor_catalog(conn, profile, clean_schema)
            elif isinstance(conn, JdbcConnectionAdapter):
                schemas, tables = self._probe_jdbc_catalog(conn, profile, clean_schema)
            elif profile.db_type == "SQLITE":
                schemas, tables = self._probe_sqlite_catalog(conn)
            elif profile.db_type in ("MYSQL", "MARIADB", "OCEANBASE", "DORIS", "STARROCKS", "TIDB"):
                schemas, tables = self._probe_mysql_catalog(conn, profile, clean_schema)
            else:
                schemas, tables = self._probe_pg_dws_catalog(conn, profile, clean_schema)

        self.store.save_catalog_snapshot(
            connection_id,
            schemas=schemas,
            tables=tables,
            target_schema=clean_schema,
        )
        self.store.add_log(
            "INFO",
            "元数据中枢",
            f"已刷新数据源 [{profile.name}] 目录缓存：{len(schemas)} 个模式，{len(tables)} 个表/视图对象",
        )
        return self.get_catalog_tree(connection_id, auto_refresh=False)

    def get_table_metadata(
        self,
        connection_id: str,
        schema_name: str,
        table_name: str,
        *,
        force_refresh: bool = False,
        penetrate_views: bool = True,
    ) -> TableMeta:
        profile = self.connection_hub.require_profile(connection_id)
        raw_t = (table_name or "").strip()
        raw_s = (schema_name or "").strip()
        if "." in raw_t and not raw_s:
            raw_s, raw_t = raw_t.split(".", 1)

        eff_schema = (
            clean_identifier_input(raw_s)
            or profile.default_schema
            or ("default" if profile.db_type == "INCEPTOR" else ("main" if profile.db_type == "SQLITE" else "public"))
        )
        eff_table = clean_identifier_input(raw_t)
        if not eff_table:
            return TableMeta(schema_name=eff_schema, table_name="", exists=False)

        if not force_refresh:
            cached = self.store.get_cached_table_meta(connection_id, eff_schema, eff_table)
            if cached is not None:
                return cached

        with self.connection_hub.session(connection_id, timeout_sec=15) as conn:
            meta = self._probe_single_table_on_conn(
                conn, profile, eff_schema, eff_table
            )

        if not meta.exists:
            return meta

        if penetrate_views and meta.object_type in ("VIEW", "MATVIEW") and (meta.view_definition or meta.raw_ddl):
            def _fetch_upstream(up_sch: str, up_tbl: str) -> Optional[TableMeta]:
                return self.get_table_metadata(
                    connection_id,
                    up_sch,
                    up_tbl,
                    force_refresh=False,
                    penetrate_views=False,
                )

            meta = enrich_view_columns_by_lineage(
                meta, fetch_object_fn=_fetch_upstream, max_depth=4
            )

        meta = self._finalize_table_meta(meta)
        self.store.save_table_meta(connection_id, meta)
        return meta

    def search_objects(
        self, keyword: str, connection_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        return self.store.search_cached_metadata(
            keyword=keyword, connection_id=connection_id, limit=limit
        )

    # =========================================================================
    # 1. PostgreSQL / 华为云 GaussDB(DWS) 定制探查器
    # =========================================================================

    def _probe_pg_dws_catalog(
        self, conn: Any, profile: ConnectionProfile, target_schema: Optional[str]
    ) -> Tuple[List[SchemaMeta], List[Dict[str, Any]]]:
        conn.autocommit = True
        with conn.cursor() as cur:
            current_schema = profile.default_schema or "public"
            try:
                cur.execute("SELECT COALESCE(current_schema(), 'public');")
                r = cur.fetchone()
                if r and r[0]:
                    current_schema = str(r[0])
            except Exception:
                pass

            if target_schema:
                schema_names = [target_schema]
            else:
                cur.execute(
                    """
                    SELECT nspname
                    FROM pg_catalog.pg_namespace
                    WHERE nspname NOT IN %s
                      AND nspname NOT LIKE 'pg_toast%%'
                      AND nspname NOT LIKE 'pg_temp%%'
                    ORDER BY
                        CASE WHEN nspname = %s THEN 0
                             WHEN nspname = 'public' THEN 1
                             ELSE 2
                        END,
                        nspname ASC;
                    """,
                    (SYSTEM_SCHEMAS, current_schema),
                )
                schema_names = [str(row[0]) for row in cur.fetchall() if row and row[0]]
                if not schema_names:
                    schema_names = [current_schema]

            where_extra = "AND LOWER(n.nspname) = LOWER(%s)" if target_schema else ""
            params: List[Any] = [SYSTEM_SCHEMAS]
            if target_schema:
                params.append(target_schema)
            params.append(current_schema)

            cur.execute(
                f"""
                SELECT
                    n.nspname AS schema_name,
                    c.relname AS table_name,
                    COALESCE(pg_catalog.obj_description(c.oid, 'pg_class'), '') AS table_comment,
                    COALESCE(pg_catalog.array_to_string(c.reloptions, ', '), '') AS reloptions,
                    c.relkind
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN %s
                  AND n.nspname NOT LIKE 'pg_toast%%'
                  AND n.nspname NOT LIKE 'pg_temp%%'
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND c.relname NOT LIKE '__mig_%%'
                  AND c.relname NOT LIKE '%%__bak_%%'
                  {where_extra}
                ORDER BY
                    CASE WHEN n.nspname = %s THEN 0
                         WHEN n.nspname = 'public' THEN 1
                         ELSE 2
                    END,
                    n.nspname ASC,
                    c.relname ASC
                LIMIT 3000;
                """,
                tuple(params),
            )
            tables: List[Dict[str, Any]] = []
            counts: Dict[str, List[int]] = {s: [0, 0] for s in schema_names}
            for row in cur.fetchall():
                sch = str(row[0])
                tbl = str(row[1])
                comm = str(row[2] or "")
                relopts = str(row[3] or "").lower()
                rk = str(row[4] or "r")
                obj_type = "VIEW" if rk == "v" else ("MATVIEW" if rk == "m" else "TABLE")
                counts.setdefault(sch, [0, 0])
                if obj_type in ("VIEW", "MATVIEW"):
                    counts[sch][1] += 1
                else:
                    counts[sch][0] += 1
                tables.append(
                    {
                        "schema_name": sch,
                        "table_name": tbl,
                        "table_comment": comm,
                        "orientation": "COLUMN" if "orientation=column" in relopts else "ROW",
                        "object_type": obj_type,
                    }
                )

            schemas = [
                SchemaMeta(schema_name=s, table_count=counts.get(s, [0, 0])[0], view_count=counts.get(s, [0, 0])[1])
                for s in counts.keys()
            ]
            return schemas, tables

    def _probe_pg_dws_table(
        self, conn: Any, profile: ConnectionProfile, schema_name: str, table_name: str
    ) -> TableMeta:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.oid,
                    n.nspname,
                    c.relname,
                    COALESCE(obj_description(c.oid, 'pg_class'), ''),
                    COALESCE(array_to_string(c.reloptions, ', '), ''),
                    c.relkind
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND LOWER(c.relname) = LOWER(%s)
                  AND n.nspname NOT IN %s
                  AND n.nspname NOT LIKE 'pg_toast%%'
                  AND n.nspname NOT LIKE 'pg_temp%%'
                ORDER BY CASE WHEN LOWER(n.nspname) = LOWER(%s) THEN 0 ELSE 1 END, n.nspname ASC
                LIMIT 1;
                """,
                (table_name, SYSTEM_SCHEMAS, schema_name),
            )
            row = cur.fetchone()
            if not row:
                return TableMeta(schema_name=schema_name, table_name=table_name, exists=False)

            rel_oid, actual_schema, actual_table, tbl_comment, reloptions_str, relkind = row
            rk = str(relkind or "r")
            obj_type = "VIEW" if rk == "v" else ("MATVIEW" if rk == "m" else "TABLE")
            orientation = "COLUMN" if "orientation=column" in str(reloptions_str or "").lower() else "ROW"
            with_clause = format_reloptions_with_clause(reloptions_str)
            dist_clause = ""
            dist_keys: List[str] = []
            part_clause = ""
            part_keys: List[str] = []
            pk_cols: List[str] = []
            raw_ddl = ""
            view_def_sql = ""

            try:
                cur.execute(
                    """
                    SELECT a.attname
                    FROM pg_catalog.pg_constraint con
                    JOIN pg_catalog.pg_attribute a
                      ON a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey)
                    WHERE con.conrelid = %s AND con.contype = 'p';
                    """,
                    (rel_oid,),
                )
                pk_cols = [str(r[0]) for r in cur.fetchall() if r and r[0]]
            except Exception:
                pk_cols = []

            if obj_type in ("VIEW", "MATVIEW"):
                try:
                    cur.execute("SELECT pg_catalog.pg_get_viewdef(%s, true);", (rel_oid,))
                    vrow = cur.fetchone()
                    if vrow and vrow[0]:
                        view_def_sql = str(vrow[0]).strip()
                        raw_ddl = (
                            f"-- [{profile.db_type} 视图原始定义 ({obj_type})]\n"
                            f"CREATE OR REPLACE VIEW {quote_ident_str(actual_schema)}.{quote_ident_str(actual_table)} AS\n"
                            f"{view_def_sql}"
                        )
                except Exception:
                    pass
            elif profile.db_type == "DWS":
                try:
                    cur.execute("SELECT pg_catalog.pg_get_tabledef(%s);", (rel_oid,))
                    tdef_row = cur.fetchone()
                    if tdef_row and tdef_row[0]:
                        raw_ddl = str(tdef_row[0]).strip()
                        parsed_def = parse_dws_tabledef(raw_ddl)
                        if parsed_def["with_clause"]:
                            with_clause = parsed_def["with_clause"]
                        if parsed_def["distribute_clause"]:
                            dist_clause = parsed_def["distribute_clause"]
                        if parsed_def["distribute_keys"]:
                            dist_keys = parsed_def["distribute_keys"]
                        if parsed_def["partition_clause"]:
                            part_clause = parsed_def["partition_clause"]
                        if parsed_def["partition_keys"]:
                            part_keys = parsed_def["partition_keys"]
                except Exception:
                    pass

                if not dist_keys:
                    try:
                        cur.execute("SELECT pg_catalog.getdistributekey(%s);", (rel_oid,))
                        dk_row = cur.fetchone()
                        if dk_row and dk_row[0]:
                            dist_keys = [
                                clean_identifier_input(x)
                                for x in str(dk_row[0]).split(",")
                                if clean_identifier_input(x)
                            ]
                    except Exception:
                        pass
            else:
                dist_keys = list(pk_cols)
                try:
                    cur.execute("SELECT pg_catalog.pg_get_partkeydef(%s);", (rel_oid,))
                    pk_row = cur.fetchone()
                    if pk_row and pk_row[0]:
                        part_clause = f"PARTITION BY {pk_row[0]}"
                        m_pk = re.search(r"\(([^)]+)\)", pk_row[0])
                        if m_pk:
                            part_keys = [
                                clean_identifier_input(x)
                                for x in m_pk.group(1).split(",")
                                if clean_identifier_input(x)
                            ]
                except Exception:
                    pass

            cur.execute(
                """
                SELECT
                    a.attname AS column_name,
                    pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                    a.attnotnull AS is_not_null,
                    pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS column_default,
                    COALESCE(col_description(c.oid, a.attnum), '') AS column_comment
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
                LEFT JOIN pg_catalog.pg_attrdef ad
                  ON ad.adrelid = c.oid AND ad.adnum = a.attnum
                WHERE c.oid = %s
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY a.attnum ASC;
                """,
                (rel_oid,),
            )
            cols = [
                ColumnMeta(
                    ordinal_position=idx,
                    column_name=str(r[0]),
                    data_type=str(r[1]),
                    is_not_null=bool(r[2]),
                    column_default=r[3],
                    column_comment=str(r[4] or ""),
                )
                for idx, r in enumerate(cur.fetchall(), start=1)
            ]

            if profile.db_type == "DWS" and part_clause and not partition_clause_has_child_defs(part_clause):
                # 自动补齐 pg_partition 起始分区边界，防止 period 分区表丢失起始分区定义
                try:
                    cur.execute(
                        """
                        SELECT relname, COALESCE(array_to_string(boundaries, ','), '')
                        FROM pg_catalog.pg_partition
                        WHERE parentid = %s AND parttype = 'p'
                        ORDER BY oid ASC;
                        """,
                        (rel_oid,),
                    )
                    p_rows = cur.fetchall()
                    if p_rows:
                        pkey_type = ""
                        if part_keys:
                            pk_col = next((c for c in cols if c.column_name.lower() == part_keys[0].lower()), None)
                            if pk_col:
                                pkey_type = pk_col.data_type
                        child_defs = []
                        for p_nm, raw_b in p_rows:
                            fmt_b = format_partition_boundary_val(str(raw_b or ""), pkey_type)
                            child_defs.append(f"PARTITION {quote_ident_str(str(p_nm))} VALUES LESS THAN ({fmt_b})")
                        part_clause = f"{part_clause.rstrip()} (\n    " + ",\n    ".join(child_defs) + "\n)"
                except Exception:
                    pass

            return TableMeta(
                schema_name=str(actual_schema),
                table_name=str(actual_table),
                exists=True,
                table_comment=str(tbl_comment or ""),
                orientation=orientation,
                with_clause=with_clause,
                distribute_clause=dist_clause,
                distribute_keys=dist_keys,
                partition_clause=part_clause,
                partition_keys=part_keys,
                primary_keys=pk_cols,
                columns=cols,
                raw_ddl=raw_ddl,
                object_type=obj_type,
                view_definition=view_def_sql,
            )

    # =========================================================================
    # 2. 星环 Transwarp Inceptor / Hive 定制探查器
    # =========================================================================

    def _probe_inceptor_catalog(
        self,
        client: InceptorThriftClient,
        profile: ConnectionProfile,
        target_schema: Optional[str],
    ) -> Tuple[List[SchemaMeta], List[Dict[str, Any]]]:
        db_rows = client.execute_query("SHOW DATABASES", max_rows=500)
        schema_names = [
            str(r[0]).strip()
            for r in db_rows
            if r and r[0] and str(r[0]).strip().lower() not in ("system", "information_schema")
        ]
        eff_db = target_schema or profile.default_schema or profile.dbname or "default"
        if eff_db not in schema_names:
            schema_names.insert(0, eff_db)

        schemas_to_scan = [target_schema] if target_schema else [eff_db]
        tables: List[Dict[str, Any]] = []
        counts: Dict[str, List[int]] = {s: [0, 0] for s in schema_names}

        for sch in schemas_to_scan:
            if not sch:
                continue
            safe_db = validate_identifier(sch, "Inceptor库名")
            tbl_rows = client.execute_query(f"SHOW TABLES IN {safe_db}", max_rows=3000)
            view_names_lower: set[str] = set()
            try:
                v_rows = client.execute_query(f"SHOW VIEWS IN {safe_db}", max_rows=3000)
                for vr in v_rows:
                    if vr:
                        v_nm = str(vr[1] if len(vr) >= 2 and str(vr[0]).lower() == safe_db.lower() else vr[0]).strip()
                        if v_nm:
                            view_names_lower.add(v_nm.lower())
            except Exception:
                pass

            seen: set[str] = set()
            for r in tbl_rows:
                if not r:
                    continue
                t_name = str(r[1] if len(r) >= 2 and str(r[0]).lower() == safe_db.lower() else r[0]).strip()
                if not t_name or t_name.lower() in seen:
                    continue
                seen.add(t_name.lower())
                is_v = t_name.lower() in view_names_lower or t_name.lower().startswith("v_")
                obj_type = "VIEW" if is_v else "TABLE"
                counts.setdefault(safe_db, [0, 0])
                counts[safe_db][1 if is_v else 0] += 1
                tables.append(
                    {
                        "schema_name": safe_db,
                        "table_name": t_name,
                        "table_comment": "[视图]" if is_v else "",
                        "object_type": obj_type,
                    }
                )

        schemas = [
            SchemaMeta(schema_name=s, table_count=counts.get(s, [0, 0])[0], view_count=counts.get(s, [0, 0])[1])
            for s in schema_names
        ]
        return schemas, tables

    def _probe_inceptor_table(
        self,
        client: InceptorThriftClient,
        profile: ConnectionProfile,
        schema_name: str,
        table_name: str,
    ) -> TableMeta:
        safe_db = validate_identifier(schema_name or profile.dbname or "default", "Inceptor库名")
        safe_tbl = validate_identifier(table_name, "Inceptor表名")
        rows: List[List[Any]] = []
        try:
            rows = client.execute_query(f"DESCRIBE FORMATTED {safe_db}.{safe_tbl}")
        except Exception:
            try:
                rows = client.execute_query(f"DESCRIBE {safe_db}.{safe_tbl}")
            except Exception:
                return TableMeta(schema_name=safe_db, table_name=safe_tbl, exists=False)

        if not rows:
            return TableMeta(schema_name=safe_db, table_name=safe_tbl, exists=False)

        cols: List[ColumnMeta] = []
        part_keys: List[str] = []
        obj_type = "TABLE"
        tbl_comment = ""
        view_def_lines: List[str] = []
        section = "COLUMNS"
        capturing_view_text = False

        for r in rows:
            c0 = str(r[0] if len(r) > 0 and r[0] is not None else "").strip()
            c1 = str(r[1] if len(r) > 1 and r[1] is not None else "").strip()
            c2 = str(r[2] if len(r) > 2 and r[2] is not None else "").strip()

            if c0.startswith("#"):
                low_hdr = c0.lower()
                capturing_view_text = False
                if "partition" in low_hdr and "not partitioned" not in low_hdr:
                    section = "PARTITION"
                elif "detailed" in low_hdr or "storage" in low_hdr or "view" in low_hdr:
                    section = "DETAILED"
                elif "col_name" in low_hdr and section != "PARTITION":
                    section = "COLUMNS"
                continue

            if section == "DETAILED":
                low_c0 = c0.lower().rstrip(":")
                if low_c0 == "table type":
                    if "view" in c1.lower():
                        obj_type = "VIEW"
                    capturing_view_text = False
                elif low_c0 in ("view expanded text", "view original text"):
                    obj_type = "VIEW"
                    capturing_view_text = True
                    if c1:
                        view_def_lines.append(c1)
                elif low_c0 in (
                    "table parameters",
                    "storage information",
                    "serde library",
                    "inputformat",
                    "outputformat",
                    "compressed",
                    "num buckets",
                    "bucket columns",
                    "sort columns",
                    "retention",
                    "create time",
                    "last access",
                    "owner",
                    "location",
                ):
                    capturing_view_text = False
                elif capturing_view_text:
                    piece = c0 or c1 or c2
                    if piece:
                        view_def_lines.append(piece)

                if c1.lower() == "comment" and c2:
                    tbl_comment = c2
                continue

            if not c0:
                continue

            if section == "PARTITION":
                if c0 not in part_keys:
                    part_keys.append(c0)
                if not any(ec.column_name.lower() == c0.lower() for ec in cols):
                    cols.append(
                        ColumnMeta(
                            ordinal_position=len(cols) + 1,
                            column_name=c0,
                            data_type=c1 or "STRING",
                            column_comment=c2,
                        )
                    )
                continue

            cols.append(
                ColumnMeta(
                    ordinal_position=len(cols) + 1,
                    column_name=c0,
                    data_type=c1,
                    column_comment=c2 if c2.lower() != "from deserializer" else "",
                )
            )

        raw_ddl = ""
        try:
            ddl_rows = client.execute_query(f"SHOW CREATE TABLE {safe_db}.{safe_tbl}")
            raw_ddl = "\n".join(str(dr[0]) for dr in ddl_rows if dr and dr[0] is not None).strip()
            if re.search(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\b", raw_ddl, flags=re.IGNORECASE):
                obj_type = "VIEW"
        except Exception:
            raw_ddl = ""

        view_def_sql = raw_ddl if obj_type == "VIEW" and raw_ddl else "\n".join(view_def_lines).strip()

        if not cols or any(not c.data_type or c.data_type.lower() == "unknown" for c in cols):
            try:
                probed_cols = client.probe_query_schema(f"SELECT * FROM {safe_db}.{safe_tbl} WHERE 1=0")
                if not cols and probed_cols:
                    cols = probed_cols
                elif probed_cols:
                    pmap = {pc.column_name.lower(): pc for pc in probed_cols}
                    for c in cols:
                        if (not c.data_type or c.data_type.lower() == "unknown") and c.column_name.lower() in pmap:
                            c.data_type = pmap[c.column_name.lower()].data_type
            except Exception:
                pass

        if not cols and view_def_sql:
            parsed_v = parse_view_sql_lineage(view_def_sql, default_schema=safe_db)
            cols = [
                ColumnMeta(
                    ordinal_position=idx,
                    column_name=vc["column_name"],
                    data_type=vc["inferred_type"],
                    column_comment=vc.get("inline_comment", ""),
                )
                for idx, vc in enumerate(parsed_v.get("columns", []), start=1)
            ]

        return TableMeta(
            schema_name=safe_db,
            table_name=safe_tbl,
            exists=True,
            table_comment=tbl_comment,
            partition_keys=part_keys,
            columns=cols,
            raw_ddl=raw_ddl,
            object_type=obj_type,
            view_definition=view_def_sql,
        )

    # =========================================================================
    # 3. MySQL / OceanBase / Doris / StarRocks / TiDB 探查器
    # =========================================================================

    def _probe_mysql_catalog(
        self, conn: Any, profile: ConnectionProfile, target_schema: Optional[str]
    ) -> Tuple[List[SchemaMeta], List[Dict[str, Any]]]:
        excluded = ("information_schema", "performance_schema", "mysql", "sys")
        with conn.cursor() as cur:
            if target_schema:
                schema_names = [target_schema]
            else:
                cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME ASC")
                schema_names = [
                    str(r[0]) for r in cur.fetchall() if r and r[0] and str(r[0]).lower() not in excluded
                ]

            if target_schema:
                cur.execute(
                    """
                    SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, COALESCE(TABLE_COMMENT, ''), COALESCE(TABLE_ROWS, 0)
                    FROM information_schema.TABLES
                    WHERE LOWER(TABLE_SCHEMA) = LOWER(%s)
                    ORDER BY TABLE_SCHEMA ASC, TABLE_NAME ASC
                    LIMIT 3000
                    """,
                    (target_schema,),
                )
            else:
                cur.execute(
                    """
                    SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, COALESCE(TABLE_COMMENT, ''), COALESCE(TABLE_ROWS, 0)
                    FROM information_schema.TABLES
                    WHERE LOWER(TABLE_SCHEMA) NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
                    ORDER BY TABLE_SCHEMA ASC, TABLE_NAME ASC
                    LIMIT 3000
                    """
                )
            counts: Dict[str, List[int]] = {s: [0, 0] for s in schema_names}
            tables: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                sch, tbl, t_type, comm, t_rows = str(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or ""), int(r[4] or 0)
                obj_type = "VIEW" if "VIEW" in t_type.upper() else "TABLE"
                counts.setdefault(sch, [0, 0])
                counts[sch][1 if obj_type == "VIEW" else 0] += 1
                tables.append(
                    {
                        "schema_name": sch,
                        "table_name": tbl,
                        "table_comment": comm,
                        "object_type": obj_type,
                        "row_count": t_rows,
                    }
                )
            schemas = [
                SchemaMeta(schema_name=s, table_count=counts.get(s, [0, 0])[0], view_count=counts.get(s, [0, 0])[1])
                for s in counts.keys()
            ]
            return schemas, tables

    def _probe_mysql_table(
        self, conn: Any, profile: ConnectionProfile, schema_name: str, table_name: str
    ) -> TableMeta:
        eff_schema = schema_name or profile.dbname or profile.default_schema
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, COALESCE(TABLE_COMMENT, '')
                FROM information_schema.TABLES
                WHERE LOWER(TABLE_SCHEMA) = LOWER(%s) AND LOWER(TABLE_NAME) = LOWER(%s)
                LIMIT 1
                """,
                (eff_schema, table_name),
            )
            t_row = cur.fetchone()
            if not t_row:
                return TableMeta(schema_name=eff_schema, table_name=table_name, exists=False)
            actual_sch, actual_tbl, t_type, tbl_comm = str(t_row[0]), str(t_row[1]), str(t_row[2] or ""), str(t_row[3] or "")
            obj_type = "VIEW" if "VIEW" in t_type.upper() else "TABLE"

            cur.execute(
                """
                SELECT ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
                       COLUMN_DEFAULT, COALESCE(COLUMN_COMMENT, ''), COLUMN_KEY
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION ASC
                """,
                (actual_sch, actual_tbl),
            )
            cols: List[ColumnMeta] = []
            pk_cols: List[str] = []
            for r in cur.fetchall():
                c_name = str(r[1])
                is_pk = str(r[6] or "").upper() == "PRI"
                if is_pk:
                    pk_cols.append(c_name)
                cols.append(
                    ColumnMeta(
                        ordinal_position=int(r[0]),
                        column_name=c_name,
                        data_type=str(r[2] or ""),
                        is_not_null=str(r[3] or "").upper() == "NO",
                        column_default=str(r[4]) if r[4] is not None else None,
                        column_comment=str(r[5] or ""),
                        is_primary_key=is_pk,
                    )
                )

            raw_ddl = ""
            view_def = ""
            try:
                q_full = f"{quote_backtick_str(actual_sch)}.{quote_backtick_str(actual_tbl)}"
                cur.execute(f"SHOW CREATE {'VIEW' if obj_type == 'VIEW' else 'TABLE'} {q_full}")
                ddl_r = cur.fetchone()
                if ddl_r and len(ddl_r) >= 2:
                    raw_ddl = str(ddl_r[1] or "")
                    if obj_type == "VIEW":
                        view_def = raw_ddl
            except Exception:
                pass

            return TableMeta(
                schema_name=actual_sch,
                table_name=actual_tbl,
                exists=True,
                table_comment=tbl_comm,
                distribute_keys=list(pk_cols),
                primary_keys=pk_cols,
                columns=cols,
                raw_ddl=raw_ddl,
                object_type=obj_type,
                view_definition=view_def,
            )

    # =========================================================================
    # 4. SQLite 3 标准库探查器
    # =========================================================================

    def _probe_sqlite_catalog(
        self, conn: Any
    ) -> Tuple[List[SchemaMeta], List[Dict[str, Any]]]:
        cur = conn.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type ASC, name ASC
            """
        )
        t_cnt = 0
        v_cnt = 0
        tables: List[Dict[str, Any]] = []
        for name, tp in cur.fetchall():
            obj_type = "VIEW" if str(tp).lower() == "view" else "TABLE"
            if obj_type == "VIEW":
                v_cnt += 1
            else:
                t_cnt += 1
            tables.append(
                {
                    "schema_name": "main",
                    "table_name": str(name),
                    "table_comment": "",
                    "object_type": obj_type,
                }
            )
        return [SchemaMeta(schema_name="main", table_count=t_cnt, view_count=v_cnt)], tables

    def _probe_sqlite_table(
        self, conn: Any, schema_name: str, table_name: str
    ) -> TableMeta:
        cur = conn.execute(
            """
            SELECT name, type, COALESCE(sql, '')
            FROM sqlite_master
            WHERE type IN ('table', 'view')
              AND LOWER(name) = LOWER(?)
            LIMIT 1
            """,
            (table_name,),
        )
        row = cur.fetchone()
        if not row:
            return TableMeta(schema_name=schema_name or "main", table_name=table_name, exists=False)
        actual_tbl, tp, raw_sql = str(row[0]), str(row[1]), str(row[2] or "")
        obj_type = "VIEW" if tp.lower() == "view" else "TABLE"

        q_tbl = '"' + actual_tbl.replace('"', '""') + '"'
        info_rows = conn.execute(f"PRAGMA table_info({q_tbl})").fetchall()
        cols: List[ColumnMeta] = []
        pk_cols: List[str] = []
        for idx, ir in enumerate(info_rows, start=1):
            # (cid, name, type, notnull, dflt_value, pk)
            c_name = str(ir[1])
            c_type = str(ir[2] or "TEXT")
            is_pk = int(ir[5] or 0) > 0
            if is_pk:
                pk_cols.append(c_name)
            cols.append(
                ColumnMeta(
                    ordinal_position=idx,
                    column_name=c_name,
                    data_type=c_type,
                    is_not_null=bool(ir[3]),
                    column_default=str(ir[4]) if ir[4] is not None else None,
                    column_comment="",
                    is_primary_key=is_pk,
                )
            )

        return TableMeta(
            schema_name="main",
            table_name=actual_tbl,
            exists=True,
            distribute_keys=list(pk_cols),
            primary_keys=pk_cols,
            columns=cols,
            raw_ddl=raw_sql,
            object_type=obj_type,
            view_definition=raw_sql if obj_type == "VIEW" else "",
        )

    # =========================================================================
    # 5. 通用自定义 JDBC DatabaseMetaData 回退探查器 (DM8 / Oracle / DB2 / GBase 等)
    # =========================================================================

    def _probe_jdbc_catalog(
        self,
        conn: JdbcConnectionAdapter,
        profile: ConnectionProfile,
        target_schema: Optional[str],
    ) -> Tuple[List[SchemaMeta], List[Dict[str, Any]]]:
        dbmd = conn.raw_jdbc_conn.getMetaData()
        schema_names: List[str] = []

        if target_schema:
            schema_names = [target_schema]
        else:
            try:
                rs_sch = dbmd.getSchemas()
                try:
                    while rs_sch.next():
                        sn = str(rs_sch.getString(1) or "").strip()
                        if sn and sn.lower() not in SYSTEM_SCHEMAS:
                            schema_names.append(sn)
                finally:
                    rs_sch.close()
            except Exception:
                pass
            if not schema_names:
                try:
                    rs_cat = dbmd.getCatalogs()
                    try:
                        while rs_cat.next():
                            cn = str(rs_cat.getString(1) or "").strip()
                            if cn:
                                schema_names.append(cn)
                    finally:
                        rs_cat.close()
                except Exception:
                    pass
            if not schema_names:
                schema_names = [profile.default_schema or profile.user or "PUBLIC"]

        counts: Dict[str, List[int]] = {s: [0, 0] for s in schema_names}
        tables: List[Dict[str, Any]] = []
        sch_pattern = target_schema if target_schema else None
        rs_tbl = dbmd.getTables(None, sch_pattern, "%", ["TABLE", "VIEW", "MATERIALIZED VIEW"])
        try:
            fetched = 0
            while rs_tbl.next() and fetched < 3000:
                t_cat = str(rs_tbl.getString("TABLE_CAT") or "").strip()
                t_sch = str(rs_tbl.getString("TABLE_SCHEM") or t_cat or profile.default_schema or "PUBLIC").strip()
                if t_sch.lower() in SYSTEM_SCHEMAS:
                    continue
                t_name = str(rs_tbl.getString("TABLE_NAME") or "").strip()
                t_type_raw = str(rs_tbl.getString("TABLE_TYPE") or "TABLE").upper()
                remarks = str(rs_tbl.getString("REMARKS") or "").strip()
                if not t_name:
                    continue
                obj_type = "MATVIEW" if "MATERIALIZED" in t_type_raw else ("VIEW" if "VIEW" in t_type_raw else "TABLE")
                counts.setdefault(t_sch, [0, 0])
                counts[t_sch][1 if obj_type in ("VIEW", "MATVIEW") else 0] += 1
                tables.append(
                    {
                        "schema_name": t_sch,
                        "table_name": t_name,
                        "table_comment": remarks,
                        "object_type": obj_type,
                    }
                )
                fetched += 1
        finally:
            rs_tbl.close()

        schemas = [
            SchemaMeta(schema_name=s, table_count=counts.get(s, [0, 0])[0], view_count=counts.get(s, [0, 0])[1])
            for s in counts.keys()
        ]
        return schemas, tables

    def _probe_jdbc_table(
        self,
        conn: JdbcConnectionAdapter,
        profile: ConnectionProfile,
        schema_name: str,
        table_name: str,
    ) -> TableMeta:
        dbmd = conn.raw_jdbc_conn.getMetaData()

        # 支持大小写自适应匹配（Oracle/DM8 常存储为大写表名）
        candidates = [(schema_name, table_name), (schema_name.upper(), table_name.upper())]
        actual_sch = schema_name
        actual_tbl = table_name
        obj_type = "TABLE"
        tbl_comm = ""
        found_tbl = False

        for cand_s, cand_t in candidates:
            rs_t = dbmd.getTables(None, cand_s or None, cand_t, ["TABLE", "VIEW", "MATERIALIZED VIEW"])
            try:
                if rs_t.next():
                    found_tbl = True
                    actual_sch = str(rs_t.getString("TABLE_SCHEM") or rs_t.getString("TABLE_CAT") or cand_s or "PUBLIC")
                    actual_tbl = str(rs_t.getString("TABLE_NAME") or cand_t)
                    tp_raw = str(rs_t.getString("TABLE_TYPE") or "TABLE").upper()
                    obj_type = "MATVIEW" if "MATERIALIZED" in tp_raw else ("VIEW" if "VIEW" in tp_raw else "TABLE")
                    tbl_comm = str(rs_t.getString("REMARKS") or "")
                    break
            finally:
                rs_t.close()

        if not found_tbl:
            return TableMeta(schema_name=schema_name, table_name=table_name, exists=False)

        pk_cols: List[str] = []
        try:
            rs_pk = dbmd.getPrimaryKeys(None, actual_sch, actual_tbl)
            try:
                while rs_pk.next():
                    pk_nm = str(rs_pk.getString("COLUMN_NAME") or "").strip()
                    if pk_nm and pk_nm not in pk_cols:
                        pk_cols.append(pk_nm)
            finally:
                rs_pk.close()
        except Exception:
            pass

        cols: List[ColumnMeta] = []
        rs_col = dbmd.getColumns(None, actual_sch, actual_tbl, "%")
        try:
            while rs_col.next():
                c_name = str(rs_col.getString("COLUMN_NAME") or "").strip()
                type_name = str(rs_col.getString("TYPE_NAME") or "VARCHAR").strip()
                col_size = int(rs_col.getInt("COLUMN_SIZE") or 0)
                dec_digits = int(rs_col.getInt("DECIMAL_DIGITS") or 0)
                nullable_flag = int(rs_col.getInt("NULLABLE"))
                col_def = rs_col.getString("COLUMN_DEF")
                remarks = str(rs_col.getString("REMARKS") or "").strip()
                ord_pos = int(rs_col.getInt("ORDINAL_POSITION") or (len(cols) + 1))

                up_t = type_name.upper()
                if "(" not in type_name:
                    if up_t in ("VARCHAR", "VARCHAR2", "NVARCHAR", "NVARCHAR2", "CHAR", "NCHAR") and 0 < col_size <= 65535:
                        type_name = f"{type_name}({col_size})"
                    elif up_t in ("DECIMAL", "NUMERIC", "NUMBER") and 0 < col_size <= 38:
                        type_name = f"{type_name}({col_size},{max(0, dec_digits)})"

                cols.append(
                    ColumnMeta(
                        ordinal_position=ord_pos,
                        column_name=c_name,
                        data_type=type_name,
                        is_not_null=(nullable_flag == 0),
                        column_default=str(col_def) if col_def is not None else None,
                        column_comment=remarks,
                        is_primary_key=(c_name in pk_cols),
                    )
                )
        finally:
            rs_col.close()

        cols.sort(key=lambda c: c.ordinal_position)
        return TableMeta(
            schema_name=actual_sch,
            table_name=actual_tbl,
            exists=True,
            table_comment=tbl_comm,
            distribute_keys=list(pk_cols),
            primary_keys=pk_cols,
            columns=cols,
            object_type=obj_type,
        )

    def _probe_single_table_on_conn(
        self,
        conn: Any,
        profile: ConnectionProfile,
        schema_name: str,
        table_name: str,
    ) -> TableMeta:
        if isinstance(conn, InceptorThriftClient):
            return self._probe_inceptor_table(conn, profile, schema_name, table_name)
        if isinstance(conn, JdbcConnectionAdapter):
            return self._probe_jdbc_table(conn, profile, schema_name, table_name)
        if profile.db_type == "SQLITE":
            return self._probe_sqlite_table(conn, schema_name, table_name)
        if profile.db_type in ("MYSQL", "MARIADB", "OCEANBASE", "DORIS", "STARROCKS", "TIDB"):
            return self._probe_mysql_table(conn, profile, schema_name, table_name)
        return self._probe_pg_dws_table(conn, profile, schema_name, table_name)
