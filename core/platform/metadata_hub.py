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
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

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
from core.platform.import_progress import ProgressCallback, noop_progress
from core.platform.inceptor_client import InceptorThriftClient
from core.platform.models import ColumnMeta, ConnectionProfile, SchemaMeta, TableMeta
from core.platform.storage import PlatformStateStore

logger = logging.getLogger("dataog")


class MetadataHub:
    """跨数据源统一元数据探查、四层视图血缘补全与 SQLite 本地索引中心."""

    def __init__(self, store: PlatformStateStore, connection_hub: ConnectionHub) -> None:
        self.store = store
        self.connection_hub = connection_hub
        self._neg_lock = threading.Lock()
        self._missing_table_cache: Dict[Tuple[str, str, str], float] = {}
        self._neg_ttl_sec = 120.0

    def invalidate_negative_cache(self, connection_id: Optional[str] = None) -> None:
        with self._neg_lock:
            if connection_id is None:
                self._missing_table_cache.clear()
                return
            doomed = [k for k in self._missing_table_cache if k[0] == connection_id]
            for k in doomed:
                self._missing_table_cache.pop(k, None)

    @staticmethod
    def _finalize_table_meta(meta: TableMeta) -> TableMeta:
        pk_lower = {k.lower() for k in (meta.primary_keys or [])}
        for col in meta.columns:
            if not col.normalized_type:
                col.normalized_type = normalize_cross_type(col.data_type)
            if col.column_name.lower() in pk_lower:
                col.is_primary_key = True
        return meta

    @staticmethod
    def _enable_pg_autocommit(conn: Any) -> None:
        if not hasattr(conn, "autocommit") or getattr(conn, "autocommit", False):
            return
        try:
            if hasattr(conn, "rollback"):
                conn.rollback()
        except Exception:
            pass
        try:
            conn.autocommit = True
        except Exception:
            pass

    @staticmethod
    def _rollback_pg_if_needed(conn: Any) -> None:
        try:
            if hasattr(conn, "rollback"):
                conn.rollback()
        except Exception:
            pass

    @classmethod
    def _apply_pg_probe_timeouts(
        cls, conn: Any, cur: Any, profile: ConnectionProfile
    ) -> None:
        """尽力而为设置 PG / GaussDB(DWS) 会话级锁等待与语句超时，防止远端 ETL 排他锁导致元数据探查无限阻塞."""
        try:
            cur.execute("SET lock_timeout = 3000;")
        except Exception:
            cls._rollback_pg_if_needed(conn)
            if profile.db_type == "DWS":
                try:
                    cur.execute("SET lockwait_timeout = 3000;")
                except Exception:
                    cls._rollback_pg_if_needed(conn)
        else:
            if profile.db_type == "DWS":
                try:
                    cur.execute("SET lockwait_timeout = 3000;")
                except Exception:
                    cls._rollback_pg_if_needed(conn)
        try:
            cur.execute("SET statement_timeout = 8000;")
        except Exception:
            cls._rollback_pg_if_needed(conn)

    @classmethod
    def _recover_pg_tx(
        cls, conn: Any, cur: Any, profile: ConnectionProfile
    ) -> None:
        cls._rollback_pg_if_needed(conn)
        if not getattr(conn, "autocommit", False):
            cls._apply_pg_probe_timeouts(conn, cur, profile)

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
        cached["default_schema"] = profile.effective_default_schema() if profile else "public"
        cached["db_type"] = profile.db_type if profile else ""
        return cached

    def refresh_catalog(
        self,
        connection_id: str,
        schema_name: Optional[str] = None,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        profile = self.connection_hub.require_profile(connection_id)
        clean_schema = clean_identifier_input(schema_name or "") or None
        self.invalidate_negative_cache(connection_id)

        progress_cb(
            18,
            "连接数据源",
            f"正在建立到 {profile.name} ({profile.db_type}) 的探查会话...",
            0,
            1,
        )
        with self.connection_hub.probe_session(connection_id, timeout_sec=15) as conn:
            progress_cb(
                45,
                "扫描系统目录",
                f"正在扫描 {clean_schema or '全部模式'} 表与视图清单...",
                0,
                1,
            )
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

        progress_cb(
            82,
            "写入本地缓存",
            f"发现 {len(schemas)} 个模式、{len(tables)} 个对象，正在落盘缓存...",
            len(tables),
            len(tables),
        )
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
        progress_cb: ProgressCallback = noop_progress,
        _shared_conn: Any = None,
    ) -> TableMeta:
        profile = self.connection_hub.require_profile(connection_id)
        raw_t = (table_name or "").strip()
        raw_s = (schema_name or "").strip()
        if "." in raw_t and not raw_s:
            raw_s, raw_t = raw_t.split(".", 1)

        default_sch = profile.effective_default_schema()
        cleaned_s = clean_identifier_input(raw_s)
        if (
            cleaned_s.lower() == "public"
            and default_sch.lower() != "public"
            and profile.db_type in (
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
            eff_schema = default_sch
        else:
            eff_schema = cleaned_s or default_sch
        eff_table = clean_identifier_input(raw_t)
        if not eff_table:
            return TableMeta(schema_name=eff_schema, table_name="", exists=False)

        neg_key = (connection_id, eff_schema.lower(), eff_table.lower())
        if not force_refresh:
            cached = self.store.get_cached_table_meta(connection_id, eff_schema, eff_table)
            if cached is not None:
                progress_cb(
                    100,
                    "命中缓存",
                    f"已从本地缓存读取 {eff_schema}.{eff_table} ({len(cached.columns)} 列)",
                    1,
                    1,
                )
                return cached
            if not cleaned_s or cleaned_s.lower() in (default_sch.lower(), "public"):
                fb_map = self.store.get_cached_tables_meta_batch(
                    connection_id,
                    [(eff_schema, eff_table)],
                    fallback_default_schema=default_sch,
                )
                fb_hit = fb_map.get((eff_schema.lower(), eff_table.lower()))
                if fb_hit is not None:
                    progress_cb(
                        100,
                        "命中缓存",
                        f"已从本地缓存读取 {fb_hit.schema_name}.{fb_hit.table_name} ({len(fb_hit.columns)} 列)",
                        1,
                        1,
                    )
                    return fb_hit

            now_mono = time.monotonic()
            hit_missing = False
            with self._neg_lock:
                miss_ts = self._missing_table_cache.get(neg_key)
                if miss_ts is not None:
                    if (now_mono - miss_ts) <= self._neg_ttl_sec:
                        hit_missing = True
                    else:
                        self._missing_table_cache.pop(neg_key, None)
            if hit_missing:
                progress_cb(
                    100,
                    "命中缓存",
                    f"已知远端暂未建表 {eff_schema}.{eff_table}",
                    1,
                    1,
                )
                return TableMeta(schema_name=eff_schema, table_name=eff_table, exists=False)
        else:
            with self._neg_lock:
                self._missing_table_cache.pop(neg_key, None)

        def _probe_and_enrich(conn: Any) -> TableMeta:
            if _shared_conn is None:
                progress_cb(
                    35,
                    "扫描系统目录",
                    f"正在读取 {eff_schema}.{eff_table} 字段与 DDL 结构...",
                    0,
                    1,
                )
            meta = self._probe_single_table_on_conn(
                conn, profile, eff_schema, eff_table
            )
            if not meta.exists:
                with self._neg_lock:
                    self._missing_table_cache[neg_key] = time.monotonic()
                if _shared_conn is None:
                    progress_cb(
                        100,
                        "探查完成",
                        f"远端不存在对象 {eff_schema}.{eff_table}",
                        1,
                        1,
                    )
                return meta

            with self._neg_lock:
                self._missing_table_cache.pop(neg_key, None)

            if (
                penetrate_views
                and meta.object_type in ("VIEW", "MATVIEW")
                and (meta.view_definition or meta.raw_ddl)
            ):
                def _fetch_upstream(up_sch: str, up_tbl: str) -> Optional[TableMeta]:
                    progress_cb(
                        62,
                        "视图穿透",
                        f"正在穿透上游依赖 {up_sch}.{up_tbl}...",
                        0,
                        1,
                    )
                    return self.get_table_metadata(
                        connection_id,
                        up_sch,
                        up_tbl,
                        force_refresh=False,
                        penetrate_views=False,
                        progress_cb=noop_progress,
                        _shared_conn=conn,
                    )

                meta = enrich_view_columns_by_lineage(
                    meta, fetch_object_fn=_fetch_upstream, max_depth=4
                )

            meta = self._finalize_table_meta(meta)
            if _shared_conn is None:
                progress_cb(
                    88,
                    "写入本地缓存",
                    f"正在保存 {meta.schema_name}.{meta.table_name} 元数据...",
                    1,
                    1,
                )
            self.store.save_table_meta(connection_id, meta)
            return meta

        if _shared_conn is not None:
            return _probe_and_enrich(_shared_conn)

        progress_cb(
            18,
            "连接数据源",
            f"正在建立到 {profile.name} ({profile.db_type}) 的探查会话...",
            0,
            1,
        )
        with self.connection_hub.probe_session(connection_id, timeout_sec=15) as conn:
            return _probe_and_enrich(conn)

    def get_tables_metadata_batch(
        self,
        connection_id: str,
        targets: Iterable[Tuple[str, str]],
        *,
        force_refresh: bool = False,
        penetrate_views: bool = True,
        tolerate_errors: bool = False,
        on_progress: Optional[Callable[[int, int, str, str], None]] = None,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Tuple[Dict[Tuple[str, str], TableMeta], str, Set[Tuple[str, str]]]:
        """单会话批量获取多张库表/视图元数据（命中本地缓存零连库，未缓存表复用单条连接批量探查并单事务落库）."""
        profile = self.connection_hub.require_profile(connection_id)
        default_sch = profile.effective_default_schema()

        norm_targets: List[Tuple[str, str]] = []
        req_to_lookup: Dict[Tuple[str, str], Tuple[str, str]] = {}
        seen_lookup = set()

        for raw_s, raw_t in targets:
            rt = (raw_t or "").strip()
            rs = (raw_s or "").strip()
            if "." in rt and not rs:
                rs, rt = rt.split(".", 1)
            eff_t = clean_identifier_input(rt)
            if not eff_t:
                continue
            cleaned_s = clean_identifier_input(rs)
            orig_s = cleaned_s or default_sch
            if (
                orig_s.lower() == "public"
                and default_sch.lower() != "public"
                and profile.db_type in (
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
                eff_s = default_sch
            elif profile.db_type == "SQLITE":
                eff_s = "main"
            else:
                eff_s = orig_s
            req_key = (orig_s.lower(), eff_t.lower())
            lookup_key = (eff_s.lower(), eff_t.lower())
            req_to_lookup[req_key] = lookup_key
            if not cleaned_s:
                req_to_lookup[("", eff_t.lower())] = lookup_key
            if lookup_key not in seen_lookup:
                seen_lookup.add(lookup_key)
                norm_targets.append((eff_s, eff_t))

        if not norm_targets:
            return {}, "", set()

        total_cnt = len(norm_targets)
        by_lookup: Dict[Tuple[str, str], TableMeta] = {}

        if not force_refresh:
            cached_map = self.store.get_cached_tables_meta_batch(
                connection_id,
                norm_targets,
                fallback_default_schema=default_sch,
            )
            for lk, c_meta in cached_map.items():
                by_lookup[lk] = c_meta

            now_mono = time.monotonic()
            with self._neg_lock:
                for s, t in norm_targets:
                    lk = (s.lower(), t.lower())
                    if lk in by_lookup:
                        continue
                    neg_key = (connection_id, lk[0], lk[1])
                    miss_ts = self._missing_table_cache.get(neg_key)
                    if miss_ts is not None:
                        if (now_mono - miss_ts) <= self._neg_ttl_sec:
                            by_lookup[lk] = TableMeta(schema_name=s, table_name=t, exists=False)
                        else:
                            self._missing_table_cache.pop(neg_key, None)
        else:
            with self._neg_lock:
                for s, t in norm_targets:
                    self._missing_table_cache.pop((connection_id, s.lower(), t.lower()), None)

        uncached = [
            (s, t) for s, t in norm_targets if (s.lower(), t.lower()) not in by_lookup
        ]
        done_cached = total_cnt - len(uncached)

        if not uncached:
            if on_progress:
                last_s, last_t = norm_targets[-1]
                on_progress(total_cnt, total_cnt, last_s, last_t)
            progress_cb(
                100,
                "命中缓存",
                f"已从本地缓存读取 {total_cnt} 张对象元数据",
                total_cnt,
                total_cnt,
            )
            out_cached = {rk: by_lookup[lk] for rk, lk in req_to_lookup.items() if lk in by_lookup}
            for lk, m in by_lookup.items():
                out_cached.setdefault(lk, m)
            return out_cached, "", set()

        def _report_batch_progress(done_n: int, tot_n: int, cur_s: str, cur_t: str) -> None:
            if on_progress:
                on_progress(done_n, tot_n, cur_s, cur_t)
            if progress_cb is not noop_progress:
                ratio = (done_n / max(1, tot_n))
                pct = min(85.0, 28.0 + ratio * 56.0)
                progress_cb(
                    pct,
                    "扫描系统目录",
                    f"正在读取 {cur_s}.{cur_t} ({done_n}/{tot_n})",
                    done_n,
                    tot_n,
                )

        progress_cb(
            15,
            "连接数据源",
            f"正在建立到 {profile.name} ({profile.db_type}) 的探查会话...",
            done_cached,
            total_cnt,
        )
        _report_batch_progress(done_cached, total_cnt, uncached[0][0], uncached[0][1])

        newly_probed: List[TableMeta] = []
        probe_warning = ""
        skipped_lookup_keys: Set[Tuple[str, str]] = set()
        probed_pairs: List[Tuple[Tuple[str, str], TableMeta]] = []
        conn_timeout = 6 if tolerate_errors else 15

        try:
            with self.connection_hub.probe_session(connection_id, timeout_sec=conn_timeout) as conn:
                used_native_batch = False

                if (
                    len(uncached) > 1
                    and not isinstance(conn, (InceptorThriftClient, JdbcConnectionAdapter))
                    and profile.db_type not in (
                        "SQLITE",
                        "MYSQL",
                        "MARIADB",
                        "OCEANBASE",
                        "DORIS",
                        "STARROCKS",
                        "TIDB",
                    )
                ):
                    try:
                        probed_pairs = self._probe_pg_dws_tables_batch(
                            conn,
                            profile,
                            uncached,
                            done_offset=done_cached,
                            total_count=total_cnt,
                            on_progress=_report_batch_progress,
                        )
                        used_native_batch = True
                    except Exception as batch_exc:
                        logger.debug("PG/DWS 批量目录探查回退单连接逐表模式: %s", batch_exc)
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        probed_pairs = []
                        used_native_batch = False
                elif (
                    len(uncached) > 1
                    and not isinstance(conn, (InceptorThriftClient, JdbcConnectionAdapter))
                    and profile.db_type in (
                        "MYSQL",
                        "MARIADB",
                        "OCEANBASE",
                        "DORIS",
                        "STARROCKS",
                        "TIDB",
                    )
                ):
                    try:
                        probed_pairs = self._probe_mysql_tables_batch(
                            conn,
                            profile,
                            uncached,
                            done_offset=done_cached,
                            total_count=total_cnt,
                            on_progress=_report_batch_progress,
                        )
                        used_native_batch = True
                    except Exception as batch_exc:
                        logger.debug("MySQL 批量目录探查回退单连接逐表模式: %s", batch_exc)
                        probed_pairs = []
                        used_native_batch = False

                if not used_native_batch:
                    consecutive_failures = 0
                    max_consecutive_failures = 3
                    for idx_u, (u_sch, u_tbl) in enumerate(uncached, start=1):
                        _report_batch_progress(done_cached + idx_u - 1, total_cnt, u_sch, u_tbl)
                        try:
                            m = self._probe_single_table_on_conn(conn, profile, u_sch, u_tbl)
                            probed_pairs.append(((u_sch.lower(), u_tbl.lower()), m))
                            consecutive_failures = 0
                        except Exception as single_exc:
                            if not tolerate_errors:
                                raise
                            consecutive_failures += 1
                            if not probe_warning:
                                probe_warning = str(single_exc)
                            skipped_lookup_keys.add((u_sch.lower(), u_tbl.lower()))
                            self._rollback_pg_if_needed(conn)
                            if (
                                consecutive_failures >= max_consecutive_failures
                                and idx_u < len(uncached)
                            ):
                                for rem_sch, rem_tbl in uncached[idx_u:]:
                                    skipped_lookup_keys.add((rem_sch.lower(), rem_tbl.lower()))
                                logger.warning(
                                    "数据源 [%s] 逐表探查连续 %d 次异常 (%s)，已触发熔断跳过剩余 %d 张未缓存表",
                                    profile.name,
                                    consecutive_failures,
                                    probe_warning,
                                    len(uncached) - idx_u,
                                )
                                last_s, last_t = uncached[-1]
                                _report_batch_progress(
                                    done_cached + len(uncached),
                                    total_cnt,
                                    last_s,
                                    last_t,
                                )
                                break
                        _report_batch_progress(done_cached + idx_u, total_cnt, u_sch, u_tbl)

                upstream_failures = 0
                for lk, meta in probed_pairs:
                    if not meta.exists:
                        by_lookup[lk] = meta
                        continue
                    if (
                        penetrate_views
                        and meta.object_type in ("VIEW", "MATVIEW")
                        and (meta.view_definition or meta.raw_ddl)
                    ):
                        def _fetch_up_on_same_conn(up_s: str, up_t: str) -> Optional[TableMeta]:
                            nonlocal upstream_failures
                            eff_us = "main" if profile.db_type == "SQLITE" else (clean_identifier_input(up_s) or default_sch)
                            eff_ut = clean_identifier_input(up_t)
                            if not eff_ut:
                                return None
                            uk = (eff_us.lower(), eff_ut.lower())
                            if uk in by_lookup:
                                return by_lookup[uk]
                            c_up = self.store.get_cached_table_meta(connection_id, eff_us, eff_ut)
                            if c_up is not None:
                                by_lookup[uk] = c_up
                                return c_up
                            if tolerate_errors and upstream_failures >= 3:
                                return None
                            try:
                                p_up = self._finalize_table_meta(
                                    self._probe_single_table_on_conn(conn, profile, eff_us, eff_ut)
                                )
                                upstream_failures = 0
                                by_lookup[uk] = p_up
                                if p_up.exists:
                                    newly_probed.append(p_up)
                                return p_up
                            except Exception:
                                upstream_failures += 1
                                self._rollback_pg_if_needed(conn)
                                return None

                        meta = enrich_view_columns_by_lineage(
                            meta, fetch_object_fn=_fetch_up_on_same_conn, max_depth=4
                        )

                    meta = self._finalize_table_meta(meta)
                    by_lookup[lk] = meta
                    newly_probed.append(meta)
        except Exception as conn_exc:
            if not tolerate_errors:
                raise
            probe_warning = str(conn_exc)
            for u_sch, u_tbl in uncached:
                lk = (u_sch.lower(), u_tbl.lower())
                if lk not in by_lookup:
                    skipped_lookup_keys.add(lk)

        now_done = time.monotonic()
        with self._neg_lock:
            for lk, meta in probed_pairs:
                neg_k = (connection_id, lk[0], lk[1])
                if not meta.exists and lk not in skipped_lookup_keys:
                    self._missing_table_cache[neg_k] = now_done
                elif meta.exists:
                    self._missing_table_cache.pop(neg_k, None)

        if newly_probed:
            progress_cb(
                90,
                "写入本地缓存",
                f"正在保存 {len(newly_probed)} 张表元数据至本地缓存...",
                total_cnt,
                total_cnt,
            )
            self.store.save_tables_meta_batch(connection_id, newly_probed)

        out_all: Dict[Tuple[str, str], TableMeta] = {}
        skipped_req_keys: Set[Tuple[str, str]] = set(skipped_lookup_keys)
        for rk, lk in req_to_lookup.items():
            if lk in skipped_lookup_keys:
                skipped_req_keys.add(rk)
            elif lk in by_lookup:
                out_all[rk] = by_lookup[lk]
        for lk, m in by_lookup.items():
            out_all.setdefault(lk, m)
        return out_all, probe_warning, skipped_req_keys

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
        self._enable_pg_autocommit(conn)
        with conn.cursor() as cur:
            current_schema = profile.default_schema or "public"
            try:
                cur.execute("SELECT COALESCE(current_schema(), 'public');")
                r = cur.fetchone()
                if r and r[0]:
                    current_schema = str(r[0])
            except Exception:
                self._rollback_pg_if_needed(conn)

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
        self._enable_pg_autocommit(conn)
        def_sch_low = profile.effective_default_schema().lower()
        strict_schema = bool(schema_name and schema_name.lower() not in (def_sch_low, "public"))
        with conn.cursor() as cur:
            self._apply_pg_probe_timeouts(conn, cur, profile)
            if strict_schema:
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
                      AND LOWER(n.nspname) = LOWER(%s)
                      AND n.nspname NOT IN %s
                      AND n.nspname NOT LIKE 'pg_toast%%'
                      AND n.nspname NOT LIKE 'pg_temp%%'
                    LIMIT 1;
                    """,
                    (table_name, schema_name, SYSTEM_SCHEMAS),
                )
            else:
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
                self._rollback_pg_if_needed(conn)
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
                    self._rollback_pg_if_needed(conn)
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
                    self._rollback_pg_if_needed(conn)

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
                        self._rollback_pg_if_needed(conn)
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
                    self._rollback_pg_if_needed(conn)

            try:
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
            except Exception:
                self._recover_pg_tx(conn, cur, profile)
                cur.execute(
                    """
                    SELECT
                        a.attname AS column_name,
                        pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                        a.attnotnull AS is_not_null,
                        NULL AS column_default,
                        COALESCE(col_description(c.oid, a.attnum), '') AS column_comment
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
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
                    self._rollback_pg_if_needed(conn)

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

    def _probe_pg_dws_tables_batch(
        self,
        conn: Any,
        profile: ConnectionProfile,
        targets: List[Tuple[str, str]],
        *,
        done_offset: int = 0,
        total_count: int = 0,
        on_progress: Optional[Callable[[int, int, str, str], None]] = None,
    ) -> List[Tuple[Tuple[str, str], TableMeta]]:
        """单连接分块批量探查 PG / GaussDB(DWS) 多表元数据，将 N*5 次查询压缩为极少数系统表批量查询."""
        self._enable_pg_autocommit(conn)
        def_sch_low = profile.effective_default_schema().lower()
        out: List[Tuple[Tuple[str, str], TableMeta]] = []
        tot = max(total_count, done_offset + len(targets))
        chunk_size = 80

        with conn.cursor() as cur:
            self._apply_pg_probe_timeouts(conn, cur, profile)
            for start_i in range(0, len(targets), chunk_size):
                batch = targets[start_i : start_i + chunk_size]
                high_water = done_offset + start_i

                def _emit_chunk_progress(step_in_chunk: int, cur_s: str, cur_t: str) -> None:
                    nonlocal high_water
                    if not on_progress:
                        return
                    target_done = min(
                        done_offset + start_i + len(batch),
                        max(high_water, done_offset + start_i + step_in_chunk),
                    )
                    high_water = target_done
                    on_progress(target_done, tot, cur_s, cur_t)

                if on_progress and batch:
                    _emit_chunk_progress(0, batch[0][0], batch[0][1])

                lower_names = tuple({t.lower() for _, t in batch if t})
                if not lower_names:
                    continue

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
                      AND LOWER(c.relname) IN %s
                      AND n.nspname NOT IN %s
                      AND n.nspname NOT LIKE 'pg_toast%%'
                      AND n.nspname NOT LIKE 'pg_temp%%'
                    ORDER BY n.nspname ASC;
                    """,
                    (lower_names, SYSTEM_SCHEMAS),
                )
                cands_by_rel: Dict[str, List[Tuple[Any, ...]]] = {}
                for r in cur.fetchall():
                    cands_by_rel.setdefault(str(r[2]).lower(), []).append(r)

                matched_for_req: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
                oid_set: Dict[Any, Tuple[Any, ...]] = {}
                for req_s, req_t in batch:
                    cands = cands_by_rel.get(req_t.lower()) or []
                    if not cands:
                        continue
                    picked = next(
                        (c for c in cands if str(c[1]).lower() == req_s.lower()),
                        None,
                    )
                    if picked is None and (not req_s or req_s.lower() in (def_sch_low, "public")):
                        picked = cands[0]
                    if picked is None:
                        continue
                    matched_for_req[(req_s.lower(), req_t.lower())] = picked
                    oid_set[picked[0]] = picked

                if not oid_set:
                    for idx_b, (req_s, req_t) in enumerate(batch, start=1):
                        out.append(
                            (
                                (req_s.lower(), req_t.lower()),
                                TableMeta(schema_name=req_s, table_name=req_t, exists=False),
                            )
                        )
                    if on_progress:
                        last_s, last_t = batch[-1]
                        _emit_chunk_progress(len(batch), last_s, last_t)
                    continue

                oid_tuple = tuple(oid_set.keys())
                pk_by_oid: Dict[Any, List[str]] = {}
                try:
                    cur.execute(
                        """
                        SELECT con.conrelid, a.attname
                        FROM pg_catalog.pg_constraint con
                        JOIN pg_catalog.pg_attribute a
                          ON a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey)
                        WHERE con.conrelid IN %s AND con.contype = 'p';
                        """,
                        (oid_tuple,),
                    )
                    for oid_v, att_nm in cur.fetchall():
                        if att_nm:
                            pk_by_oid.setdefault(oid_v, []).append(str(att_nm))
                except Exception:
                    self._recover_pg_tx(conn, cur, profile)
                    pk_by_oid = {}

                try:
                    cur.execute(
                        """
                        SELECT
                            c.oid,
                            a.attname AS column_name,
                            pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                            a.attnotnull AS is_not_null,
                            pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS column_default,
                            COALESCE(col_description(c.oid, a.attnum), '') AS column_comment
                        FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
                        LEFT JOIN pg_catalog.pg_attrdef ad
                          ON ad.adrelid = c.oid AND ad.adnum = a.attnum
                        WHERE c.oid IN %s
                          AND a.attnum > 0
                          AND NOT a.attisdropped
                        ORDER BY c.oid ASC, a.attnum ASC;
                        """,
                        (oid_tuple,),
                    )
                except Exception:
                    self._recover_pg_tx(conn, cur, profile)
                    cur.execute(
                        """
                        SELECT
                            c.oid,
                            a.attname AS column_name,
                            pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                            a.attnotnull AS is_not_null,
                            NULL AS column_default,
                            COALESCE(col_description(c.oid, a.attnum), '') AS column_comment
                        FROM pg_catalog.pg_class c
                        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
                        WHERE c.oid IN %s
                          AND a.attnum > 0
                          AND NOT a.attisdropped
                        ORDER BY c.oid ASC, a.attnum ASC;
                        """,
                        (oid_tuple,),
                    )
                cols_by_oid: Dict[Any, List[ColumnMeta]] = {}
                for oid_v, c_nm, c_tp, not_null, c_def, c_comm in cur.fetchall():
                    lst = cols_by_oid.setdefault(oid_v, [])
                    lst.append(
                        ColumnMeta(
                            ordinal_position=len(lst) + 1,
                            column_name=str(c_nm),
                            data_type=str(c_tp),
                            is_not_null=bool(not_null),
                            column_default=c_def,
                            column_comment=str(c_comm or ""),
                        )
                    )
                if on_progress and len(batch) > 1:
                    first_info = next(iter(oid_set.values()))
                    _emit_chunk_progress(
                        max(1, int(len(batch) * 0.2)),
                        str(first_info[1]),
                        str(first_info[2]),
                    )

                view_oids = tuple(
                    oid_v for oid_v, info in oid_set.items() if str(info[5] or "r") in ("v", "m")
                )
                table_oids = tuple(
                    oid_v for oid_v, info in oid_set.items() if str(info[5] or "r") not in ("v", "m")
                )

                view_def_by_oid: Dict[Any, str] = {}
                if view_oids:
                    try:
                        cur.execute(
                            "SELECT oid, pg_catalog.pg_get_viewdef(oid, true) FROM pg_catalog.pg_class WHERE oid IN %s;",
                            (view_oids,),
                        )
                        for oid_v, v_sql in cur.fetchall():
                            if v_sql:
                                view_def_by_oid[oid_v] = str(v_sql).strip()
                    except Exception:
                        self._recover_pg_tx(conn, cur, profile)
                        slow_vdef_failures = 0
                        for oid_v in view_oids:
                            t0_v = time.monotonic()
                            try:
                                cur.execute("SELECT pg_catalog.pg_get_viewdef(%s, true);", (oid_v,))
                                vr = cur.fetchone()
                                if vr and vr[0]:
                                    view_def_by_oid[oid_v] = str(vr[0]).strip()
                            except Exception as v_exc:
                                self._recover_pg_tx(conn, cur, profile)
                                if (
                                    isinstance(v_exc, (TimeoutError, ConnectionError, OSError, EOFError))
                                    or (time.monotonic() - t0_v) >= 0.8
                                ):
                                    slow_vdef_failures += 1
                                    if slow_vdef_failures >= 3:
                                        break

                tdef_by_oid: Dict[Any, str] = {}
                dkey_fallback_by_oid: Dict[Any, List[str]] = {}
                partkey_pg_by_oid: Dict[Any, str] = {}

                if table_oids and profile.db_type == "DWS":
                    sub_chunk = 15
                    slow_tdef_failures = 0
                    for sub_i in range(0, len(table_oids), sub_chunk):
                        sub_oids = table_oids[sub_i : sub_i + sub_chunk]
                        if slow_tdef_failures >= 3:
                            break
                        if on_progress and sub_oids:
                            head_info = oid_set[sub_oids[0]]
                            _emit_chunk_progress(
                                int(len(batch) * (0.2 + 0.7 * (sub_i / max(1, len(table_oids))))),
                                str(head_info[1]),
                                str(head_info[2]),
                            )
                        try:
                            cur.execute(
                                "SELECT oid, pg_catalog.pg_get_tabledef(oid) FROM pg_catalog.pg_class WHERE oid IN %s;",
                                (sub_oids,),
                            )
                            for oid_v, td_sql in cur.fetchall():
                                if td_sql:
                                    tdef_by_oid[oid_v] = str(td_sql).strip()
                        except Exception:
                            self._recover_pg_tx(conn, cur, profile)
                            for idx_so, oid_v in enumerate(sub_oids, start=1):
                                t0_td = time.monotonic()
                                try:
                                    cur.execute("SELECT pg_catalog.pg_get_tabledef(%s);", (oid_v,))
                                    tr = cur.fetchone()
                                    if tr and tr[0]:
                                        tdef_by_oid[oid_v] = str(tr[0]).strip()
                                except Exception as td_exc:
                                    self._recover_pg_tx(conn, cur, profile)
                                    if (
                                        isinstance(td_exc, (TimeoutError, ConnectionError, OSError, EOFError))
                                        or (time.monotonic() - t0_td) >= 0.8
                                    ):
                                        slow_tdef_failures += 1
                                        if slow_tdef_failures >= 3:
                                            break
                                if on_progress:
                                    so_info = oid_set[oid_v]
                                    done_so = min(len(table_oids), sub_i + idx_so)
                                    _emit_chunk_progress(
                                        int(len(batch) * (0.2 + 0.7 * (done_so / max(1, len(table_oids))))),
                                        str(so_info[1]),
                                        str(so_info[2]),
                                    )
                        else:
                            if on_progress and sub_oids:
                                tail_info = oid_set[sub_oids[-1]]
                                done_so = min(len(table_oids), sub_i + len(sub_oids))
                                _emit_chunk_progress(
                                    int(len(batch) * (0.2 + 0.7 * (done_so / max(1, len(table_oids))))),
                                    str(tail_info[1]),
                                    str(tail_info[2]),
                                )
                    if slow_tdef_failures < 3:
                        try:
                            cur.execute(
                                "SELECT oid, pg_catalog.getdistributekey(oid) FROM pg_catalog.pg_class WHERE oid IN %s;",
                                (table_oids,),
                            )
                            for oid_v, dk_raw in cur.fetchall():
                                if dk_raw:
                                    dkey_fallback_by_oid[oid_v] = [
                                        clean_identifier_input(x)
                                        for x in str(dk_raw).split(",")
                                        if clean_identifier_input(x)
                                    ]
                        except Exception:
                            self._recover_pg_tx(conn, cur, profile)
                elif table_oids:
                    try:
                        cur.execute(
                            "SELECT oid, pg_catalog.pg_get_partkeydef(oid) FROM pg_catalog.pg_class WHERE oid IN %s;",
                            (table_oids,),
                        )
                        for oid_v, pk_def in cur.fetchall():
                            if pk_def:
                                partkey_pg_by_oid[oid_v] = str(pk_def)
                    except Exception:
                        self._recover_pg_tx(conn, cur, profile)

                meta_by_oid: Dict[Any, TableMeta] = {}
                need_part_children_oids: List[Any] = []
                for oid_v, row in oid_set.items():
                    _, actual_schema, actual_table, tbl_comment, reloptions_str, relkind = row
                    rk = str(relkind or "r")
                    obj_type = "VIEW" if rk == "v" else ("MATVIEW" if rk == "m" else "TABLE")
                    orientation = "COLUMN" if "orientation=column" in str(reloptions_str or "").lower() else "ROW"
                    with_clause = format_reloptions_with_clause(reloptions_str)
                    pk_cols = list(pk_by_oid.get(oid_v, []))
                    cols = [
                        ColumnMeta(
                            ordinal_position=c.ordinal_position,
                            column_name=c.column_name,
                            data_type=c.data_type,
                            is_not_null=c.is_not_null,
                            column_default=c.column_default,
                            column_comment=c.column_comment,
                        )
                        for c in cols_by_oid.get(oid_v, [])
                    ]
                    dist_clause = ""
                    dist_keys: List[str] = []
                    part_clause = ""
                    part_keys: List[str] = []
                    raw_ddl = ""
                    view_def_sql = ""

                    if obj_type in ("VIEW", "MATVIEW"):
                        view_def_sql = view_def_by_oid.get(oid_v, "")
                        if view_def_sql:
                            raw_ddl = (
                                f"-- [{profile.db_type} 视图原始定义 ({obj_type})]\n"
                                f"CREATE OR REPLACE VIEW {quote_ident_str(str(actual_schema))}.{quote_ident_str(str(actual_table))} AS\n"
                                f"{view_def_sql}"
                            )
                    elif profile.db_type == "DWS":
                        raw_ddl = tdef_by_oid.get(oid_v, "")
                        if raw_ddl:
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
                        if not dist_keys:
                            dist_keys = list(dkey_fallback_by_oid.get(oid_v, []))
                        if part_clause and not partition_clause_has_child_defs(part_clause):
                            need_part_children_oids.append(oid_v)
                    else:
                        dist_keys = list(pk_cols)
                        pk_def = partkey_pg_by_oid.get(oid_v, "")
                        if pk_def:
                            part_clause = f"PARTITION BY {pk_def}"
                            m_pk = re.search(r"\(([^)]+)\)", pk_def)
                            if m_pk:
                                part_keys = [
                                    clean_identifier_input(x)
                                    for x in m_pk.group(1).split(",")
                                    if clean_identifier_input(x)
                                ]

                    meta_by_oid[oid_v] = TableMeta(
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

                if need_part_children_oids:
                    try:
                        cur.execute(
                            """
                            SELECT parentid, relname, COALESCE(array_to_string(boundaries, ','), '')
                            FROM pg_catalog.pg_partition
                            WHERE parentid IN %s AND parttype = 'p'
                            ORDER BY parentid ASC, oid ASC;
                            """,
                            (tuple(need_part_children_oids),),
                        )
                        parts_by_parent: Dict[Any, List[Tuple[str, str]]] = {}
                        for p_oid, p_nm, raw_b in cur.fetchall():
                            parts_by_parent.setdefault(p_oid, []).append((str(p_nm), str(raw_b or "")))
                        for p_oid, p_rows in parts_by_parent.items():
                            m_obj = meta_by_oid.get(p_oid)
                            if not m_obj or not p_rows:
                                continue
                            pkey_type = ""
                            if m_obj.partition_keys:
                                pk_col = next(
                                    (c for c in m_obj.columns if c.column_name.lower() == m_obj.partition_keys[0].lower()),
                                    None,
                                )
                                if pk_col:
                                    pkey_type = pk_col.data_type
                            child_defs = [
                                f"PARTITION {quote_ident_str(p_nm)} VALUES LESS THAN ({format_partition_boundary_val(raw_b, pkey_type)})"
                                for p_nm, raw_b in p_rows
                            ]
                            m_obj.partition_clause = (
                                f"{m_obj.partition_clause.rstrip()} (\n    " + ",\n    ".join(child_defs) + "\n)"
                            )
                    except Exception:
                        self._recover_pg_tx(conn, cur, profile)

                for idx_b, (req_s, req_t) in enumerate(batch, start=1):
                    rk = (req_s.lower(), req_t.lower())
                    picked = matched_for_req.get(rk)
                    if picked is None:
                        out.append((rk, TableMeta(schema_name=req_s, table_name=req_t, exists=False)))
                    else:
                        out.append((rk, meta_by_oid[picked[0]]))
                    if on_progress and (
                        (idx_b % 10 == 0 and done_offset + start_i + idx_b >= high_water)
                        or idx_b == len(batch)
                    ):
                        _emit_chunk_progress(idx_b, req_s, req_t)

        return out

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
        except (TimeoutError, ConnectionError, OSError, EOFError):
            raise
        except Exception:
            try:
                rows = client.execute_query(f"DESCRIBE {safe_db}.{safe_tbl}")
            except (TimeoutError, ConnectionError, OSError, EOFError):
                raise
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
                if low_c0 in ("table type", "type"):
                    if "view" in c1.lower():
                        obj_type = "VIEW"
                    capturing_view_text = False
                elif low_c0 in ("view expanded text", "view original text", "view text"):
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
                elif low_c0 == "comment" and c1:
                    tbl_comment = c1
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
        db_fallback = (profile.dbname or "").strip()
        eff_schema = schema_name or db_fallback or profile.default_schema
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
            if (
                not t_row
                and eff_schema.lower() == "public"
                and db_fallback
                and db_fallback.lower() != "public"
            ):
                eff_schema = db_fallback
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

    def _probe_mysql_tables_batch(
        self,
        conn: Any,
        profile: ConnectionProfile,
        targets: List[Tuple[str, str]],
        *,
        done_offset: int = 0,
        total_count: int = 0,
        on_progress: Optional[Callable[[int, int, str, str], None]] = None,
    ) -> List[Tuple[Tuple[str, str], TableMeta]]:
        """单连接分块批量探查 MySQL / OceanBase / Doris / StarRocks / TiDB 多表元数据."""
        out: List[Tuple[Tuple[str, str], TableMeta]] = []
        tot = max(total_count, done_offset + len(targets))
        default_sch = (profile.dbname or "").strip() or (profile.default_schema or "").strip()
        if default_sch.lower() == "public" and (profile.dbname or "").strip():
            default_sch = (profile.dbname or "").strip()
        chunk_size = 100

        with conn.cursor() as cur:
            for start_i in range(0, len(targets), chunk_size):
                batch = targets[start_i : start_i + chunk_size]
                if on_progress and batch:
                    on_progress(done_offset + start_i, tot, batch[0][0], batch[0][1])

                norm_batch = [(s or default_sch, t) for s, t in batch]
                lower_tbls = list({t.lower() for _, t in norm_batch if t})
                sch_set = {s.lower() for s, _ in norm_batch if s}
                if (
                    "public" in sch_set
                    and default_sch
                    and default_sch.lower() != "public"
                ):
                    sch_set.add(default_sch.lower())
                lower_schs = list(sch_set)
                if not lower_tbls:
                    continue

                t_ph = ",".join(["%s"] * len(lower_tbls))
                if lower_schs:
                    s_ph = ",".join(["%s"] * len(lower_schs))
                    cur.execute(
                        f"""
                        SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, COALESCE(TABLE_COMMENT, '')
                        FROM information_schema.TABLES
                        WHERE LOWER(TABLE_SCHEMA) IN ({s_ph}) AND LOWER(TABLE_NAME) IN ({t_ph})
                        """,
                        tuple(lower_schs + lower_tbls),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, COALESCE(TABLE_COMMENT, '')
                        FROM information_schema.TABLES
                        WHERE LOWER(TABLE_NAME) IN ({t_ph})
                        """,
                        tuple(lower_tbls),
                    )
                tbl_info_map: Dict[Tuple[str, str], Tuple[str, str, str, str]] = {}
                actual_schs = set()
                actual_tbls = set()
                for r in cur.fetchall():
                    a_s, a_t, t_tp, t_cm = str(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or "")
                    tbl_info_map[(a_s.lower(), a_t.lower())] = (a_s, a_t, t_tp, t_cm)
                    actual_schs.add(a_s)
                    actual_tbls.add(a_t)

                cols_map: Dict[Tuple[str, str], List[ColumnMeta]] = {}
                pks_map: Dict[Tuple[str, str], List[str]] = {}
                if actual_tbls and actual_schs:
                    as_list = list(actual_schs)
                    at_list = list(actual_tbls)
                    as_ph = ",".join(["%s"] * len(as_list))
                    at_ph = ",".join(["%s"] * len(at_list))
                    cur.execute(
                        f"""
                        SELECT TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION, COLUMN_NAME,
                               COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT,
                               COALESCE(COLUMN_COMMENT, ''), COLUMN_KEY
                        FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA IN ({as_ph}) AND TABLE_NAME IN ({at_ph})
                        ORDER BY TABLE_SCHEMA ASC, TABLE_NAME ASC, ORDINAL_POSITION ASC
                        """,
                        tuple(as_list + at_list),
                    )
                    for r in cur.fetchall():
                        mk = (str(r[0]).lower(), str(r[1]).lower())
                        c_name = str(r[3])
                        is_pk = str(r[8] or "").upper() == "PRI"
                        if is_pk:
                            pks_map.setdefault(mk, []).append(c_name)
                        cols_map.setdefault(mk, []).append(
                            ColumnMeta(
                                ordinal_position=int(r[2]),
                                column_name=c_name,
                                data_type=str(r[4] or ""),
                                is_not_null=str(r[5] or "").upper() == "NO",
                                column_default=str(r[6]) if r[6] is not None else None,
                                column_comment=str(r[7] or ""),
                                is_primary_key=is_pk,
                            )
                        )

                for idx_b, ((orig_s, orig_t), (eff_s, _)) in enumerate(zip(batch, norm_batch), start=1):
                    rk = (orig_s.lower(), orig_t.lower())
                    hit = tbl_info_map.get((eff_s.lower(), orig_t.lower()))
                    if (
                        not hit
                        and eff_s.lower() == "public"
                        and default_sch
                        and default_sch.lower() != "public"
                    ):
                        hit = tbl_info_map.get((default_sch.lower(), orig_t.lower()))
                    if not hit:
                        miss_s = default_sch if (eff_s.lower() == "public" and default_sch) else eff_s
                        out.append((rk, TableMeta(schema_name=miss_s, table_name=orig_t, exists=False)))
                    else:
                        a_s, a_t, t_tp, t_cm = hit
                        mk = (a_s.lower(), a_t.lower())
                        obj_type = "VIEW" if "VIEW" in t_tp.upper() else "TABLE"
                        raw_ddl = ""
                        view_def = ""
                        if obj_type == "VIEW":
                            try:
                                q_full = f"{quote_backtick_str(a_s)}.{quote_backtick_str(a_t)}"
                                cur.execute(f"SHOW CREATE VIEW {q_full}")
                                ddl_r = cur.fetchone()
                                if ddl_r and len(ddl_r) >= 2:
                                    raw_ddl = str(ddl_r[1] or "")
                                    view_def = raw_ddl
                            except Exception:
                                pass
                        pk_cols = pks_map.get(mk, [])
                        out.append(
                            (
                                rk,
                                TableMeta(
                                    schema_name=a_s,
                                    table_name=a_t,
                                    exists=True,
                                    table_comment=t_cm,
                                    distribute_keys=list(pk_cols),
                                    primary_keys=pk_cols,
                                    columns=cols_map.get(mk, []),
                                    raw_ddl=raw_ddl,
                                    object_type=obj_type,
                                    view_definition=view_def,
                                ),
                            )
                        )
                    if on_progress and (idx_b % 10 == 0 or idx_b == len(batch)):
                        on_progress(done_offset + start_i + idx_b, tot, orig_s, orig_t)

        return out

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
            inline_comm = ""
            if raw_sql and obj_type == "TABLE":
                m_comm = re.search(
                    rf'^\s*["`]?{re.escape(c_name)}["`]?\b[^\n]*?--\s*(.+?)\s*$',
                    raw_sql,
                    flags=re.MULTILINE | re.IGNORECASE,
                )
                if m_comm:
                    inline_comm = m_comm.group(1).strip()
            cols.append(
                ColumnMeta(
                    ordinal_position=idx,
                    column_name=c_name,
                    data_type=c_type,
                    is_not_null=bool(ir[3]),
                    column_default=str(ir[4]) if ir[4] is not None else None,
                    column_comment=inline_comm,
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
