"""DataOG 通用简单查询与表数据快速预览引擎 (QueryEngine).

核心能力：
1. 跨内置原生驱动 (DWS/PGSQL/Inceptor/MySQL/SQLite) 与自定义 JDBC 驱动统一的 SQL 执行接口；
2. 默认安全行数截断 (`LIMIT 500` + `fetchmany(limit + 1)` 游标级内存保护)，绝不拉爆本地内存；
3. 会话级与线程级查询超时保护、精确耗时统计 (`elapsed_ms`) 与 JSON 安全类型序列化；
4. 支持一键将查询结果集导出为带 UTF-8 BOM 的 CSV 或标准 Excel `.xlsx` 文件。
"""

from __future__ import annotations

import datetime
import decimal
import re
import threading
import time
from typing import Any, Dict, List, Sequence, Tuple

from core.platform.connection_hub import ConnectionHub
from core.platform.dialects import (
    quote_dialect_ident,
    strip_sql_comments_preserve_literals,
    validate_identifier,
)
from core.platform.driver_manager import JdbcConnectionAdapter
from core.platform.excel_io import export_tabular_csv_bytes, export_tabular_xlsx_bytes
from core.platform.inceptor_client import InceptorThriftClient
from core.platform.models import ConnectionProfile, QueryColumn, QueryResult
from core.platform.readonly_guard import ReadOnlyViolationError, validate_readonly_sql
from core.platform.storage import PlatformStateStore

MAX_ALLOWED_QUERY_LIMIT = 5000
DEFAULT_QUERY_LIMIT = 500
DEFAULT_READONLY_SAMPLE_LIMIT = 100
MAX_READONLY_SAMPLE_LIMIT = 200


def serialize_cell_value(val: Any) -> Any:
    """将各种数据库驱动返回的复杂 Python/Java 对象转换为前端 JSON 安全的标量."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat(sep=" ") if isinstance(val, datetime.datetime) else val.isoformat()
    if isinstance(val, decimal.Decimal):
        return str(val)
    if isinstance(val, (bytes, bytearray, memoryview)):
        raw_b = bytes(val)
        if len(raw_b) > 64:
            return f"0x{raw_b[:64].hex()}... ({len(raw_b)} bytes)"
        return f"0x{raw_b.hex()}"
    return str(val)


class QueryEngine:
    """统一 SQL 执行、表数据快速预览与结果导出中心."""

    def __init__(self, store: PlatformStateStore, connection_hub: ConnectionHub) -> None:
        self.store = store
        self.connection_hub = connection_hub

    @staticmethod
    def clamp_limit(limit: int) -> int:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = DEFAULT_QUERY_LIMIT
        return max(1, min(MAX_ALLOWED_QUERY_LIMIT, n))

    @staticmethod
    def _build_bounded_select_sql(sql: str, db_type: str, cap: int) -> str:
        """在不破坏原语义的前提下，对未显式声明 LIMIT 的单条纯查询 SELECT 自动追加行数保护上限."""
        dialect = (db_type or "").strip().upper()
        limit_dialects = {
            "DWS",
            "PGSQL",
            "KINGBASE",
            "MYSQL",
            "MARIADB",
            "OCEANBASE",
            "DORIS",
            "STARROCKS",
            "TIDB",
            "INCEPTOR",
            "SQLITE",
            "DM8",
            "GBASE",
        }
        if dialect not in limit_dialects and dialect not in ("DB2", "ORACLE"):
            return sql
        stripped = strip_sql_comments_preserve_literals(sql).strip()
        while stripped.endswith(";"):
            stripped = stripped[:-1].rstrip()
        if not stripped or ";" in stripped:
            return sql
        if not re.match(r"^(?:SELECT|WITH)\b", stripped, flags=re.IGNORECASE):
            return sql
        if re.search(
            r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|INTO|LIMIT|FETCH\s+(?:FIRST|NEXT)|ROWNUM|FOR\s+(?:UPDATE|SHARE))\b",
            stripped,
            flags=re.IGNORECASE,
        ):
            return sql
        raw_no_semi = sql.rstrip()
        while raw_no_semi.endswith(";"):
            raw_no_semi = raw_no_semi[:-1].rstrip()
        if dialect == "DB2":
            return f"{raw_no_semi}\nFETCH FIRST {int(cap)} ROWS ONLY"
        if dialect == "ORACLE":
            return f"SELECT * FROM (\n{raw_no_semi}\n) dataog_ro_sub WHERE ROWNUM <= {int(cap)}"
        return f"{raw_no_semi}\nLIMIT {int(cap)}"

    def execute_sql(
        self,
        connection_id: str,
        sql: str,
        *,
        limit: int = DEFAULT_QUERY_LIMIT,
        timeout_sec: int = 30,
    ) -> QueryResult:
        clean_sql = (sql or "").strip()
        if not clean_sql:
            raise ValueError("要执行的 SQL 语句不能为空")
        safe_limit = self.clamp_limit(limit)
        safe_timeout = max(2, min(300, int(timeout_sec or 30)))
        profile = self.connection_hub.require_profile(connection_id)

        t0 = time.perf_counter()
        try:
            with self.connection_hub.session(connection_id, timeout_sec=min(15, safe_timeout)) as conn:
                if isinstance(conn, InceptorThriftClient):
                    cols_meta, raw_rows, truncated = conn.execute_query_with_meta(
                        clean_sql, max_rows=safe_limit
                    )
                    q_cols = [
                        QueryColumn(name=c.column_name, data_type=c.data_type)
                        for c in cols_meta
                    ]
                    rows = [
                        [serialize_cell_value(cell) for cell in r]
                        for r in raw_rows
                    ]
                    affected = 0
                elif isinstance(conn, JdbcConnectionAdapter):
                    q_cols, rows, truncated, affected = self._exec_on_jdbc(
                        conn, clean_sql, safe_limit, safe_timeout
                    )
                elif profile.db_type == "SQLITE":
                    q_cols, rows, truncated, affected = self._exec_on_sqlite(
                        conn, clean_sql, safe_limit, safe_timeout
                    )
                else:
                    q_cols, rows, truncated, affected = self._exec_on_dbapi(
                        conn, profile, clean_sql, safe_limit, safe_timeout
                    )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.store.add_log(
                "ERROR",
                f"SQL查询 [{profile.name}]",
                f"SQL 执行失败 ({elapsed_ms:.1f} ms): {exc}",
                sql_text=clean_sql[:2000],
            )
            raise

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if q_cols:
            msg = (
                f"查询完成：返回 {len(rows)} 行"
                + (f"（已按上限 {safe_limit} 行安全截断）" if truncated else "")
                + f"，耗时 {elapsed_ms:.1f} ms"
            )
        else:
            msg = f"语句执行完成（影响行数: {max(0, affected)}），耗时 {elapsed_ms:.1f} ms"

        self.store.add_log(
            "INFO",
            f"SQL查询 [{profile.name}]",
            msg,
            sql_text=clean_sql[:2000],
        )
        return QueryResult(
            connection_id=connection_id,
            sql=clean_sql,
            columns=q_cols,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            limit_applied=safe_limit,
            elapsed_ms=elapsed_ms,
            affected_rows=max(0, affected),
            message=msg,
        )

    def preview_table(
        self,
        connection_id: str,
        schema_name: str,
        table_name: str,
        *,
        limit: int = 200,
    ) -> QueryResult:
        profile = self.connection_hub.require_profile(connection_id)
        safe_limit = self.clamp_limit(limit)
        eff_table = validate_identifier(table_name, "预览表名")
        eff_schema = (schema_name or profile.default_schema or "").strip()

        q_table = quote_dialect_ident(eff_table, profile.db_type)
        if profile.db_type == "SQLITE" and (not eff_schema or eff_schema.lower() == "main"):
            target_ref = q_table
        elif eff_schema:
            q_schema = quote_dialect_ident(eff_schema, profile.db_type)
            target_ref = f"{q_schema}.{q_table}"
        else:
            target_ref = q_table

        if profile.db_type == "ORACLE":
            display_sql = f"SELECT * FROM {target_ref} WHERE ROWNUM <= {safe_limit}"
            probe_sql = f"SELECT * FROM {target_ref} WHERE ROWNUM <= {safe_limit + 1}"
        elif profile.db_type == "DB2":
            display_sql = f"SELECT * FROM {target_ref} FETCH FIRST {safe_limit} ROWS ONLY"
            probe_sql = f"SELECT * FROM {target_ref} FETCH FIRST {safe_limit + 1} ROWS ONLY"
        else:
            display_sql = f"SELECT * FROM {target_ref} LIMIT {safe_limit}"
            probe_sql = f"SELECT * FROM {target_ref} LIMIT {safe_limit + 1}"

        res = self.execute_sql(connection_id, probe_sql, limit=safe_limit)
        res.sql = display_sql
        return res

    @staticmethod
    def export_result_bytes(
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        fmt: str = "csv",
        title: str = "查询结果",
    ) -> Tuple[bytes, str]:
        f_low = (fmt or "csv").strip().lower()
        if f_low in ("xlsx", "excel"):
            return (
                export_tabular_xlsx_bytes(columns, rows, sheet_title=title),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        return (
            export_tabular_csv_bytes(columns, rows),
            "text/csv; charset=utf-8",
        )

    def _exec_on_sqlite(
        self,
        conn: Any,
        sql: str,
        safe_limit: int,
        timeout_sec: int,
    ) -> Tuple[List[QueryColumn], List[List[Any]], bool, int]:
        timed_out = threading.Event()

        def _interrupt() -> None:
            timed_out.set()
            try:
                conn.interrupt()
            except Exception:
                pass

        timer = threading.Timer(float(timeout_sec), _interrupt)
        timer.daemon = True
        timer.start()
        try:
            cur = conn.execute(sql)
            try:
                if cur.description:
                    q_cols = [QueryColumn(name=str(d[0]), data_type="") for d in cur.description]
                    fetched = cur.fetchmany(safe_limit + 1)
                    truncated = len(fetched) > safe_limit
                    if truncated:
                        fetched = fetched[:safe_limit]
                    rows = [[serialize_cell_value(v) for v in r] for r in fetched]
                    conn.commit()
                    return q_cols, rows, truncated, 0
                affected = int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)
                conn.commit()
                return [], [], False, affected
            finally:
                cur.close()
        except Exception as exc:
            if timed_out.is_set():
                raise TimeoutError(f"SQLite 查询超过 {timeout_sec} 秒超时上限已自动中断") from exc
            raise
        finally:
            timer.cancel()

    def _exec_on_jdbc(
        self,
        conn: JdbcConnectionAdapter,
        sql: str,
        safe_limit: int,
        timeout_sec: int,
    ) -> Tuple[List[QueryColumn], List[List[Any]], bool, int]:
        with conn.cursor() as cur:
            cur.execute(sql, timeout_sec=timeout_sec, max_rows=safe_limit + 1)
            if cur.description:
                q_cols = [
                    QueryColumn(name=str(d[0]), data_type=str(d[1] or ""))
                    for d in cur.description
                ]
                fetched = cur.fetchmany(safe_limit + 1)
                truncated = len(fetched) > safe_limit
                if truncated:
                    fetched = fetched[:safe_limit]
                rows = [[serialize_cell_value(v) for v in r] for r in fetched]
                conn.commit()
                return q_cols, rows, truncated, 0
            affected = int(cur.rowcount if cur.rowcount >= 0 else 0)
            conn.commit()
            return [], [], False, affected

    def _exec_on_dbapi(
        self,
        conn: Any,
        profile: ConnectionProfile,
        sql: str,
        safe_limit: int,
        timeout_sec: int,
    ) -> Tuple[List[QueryColumn], List[List[Any]], bool, int]:
        cur = conn.cursor()
        try:
            if profile.db_type in ("DWS", "PGSQL", "KINGBASE"):
                try:
                    cur.execute(f"SET statement_timeout = {int(timeout_sec) * 1000};")
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            bounded_sql = self._build_bounded_select_sql(sql, profile.db_type, safe_limit + 1)
            cur.execute(bounded_sql)
            if cur.description:
                q_cols = [
                    QueryColumn(name=str(d[0]), data_type=str(d[1].__name__ if hasattr(d[1], "__name__") else ""))
                    for d in cur.description
                ]
                fetched = cur.fetchmany(safe_limit + 1)
                truncated = len(fetched) > safe_limit
                if truncated:
                    fetched = fetched[:safe_limit]
                rows = [[serialize_cell_value(v) for v in r] for r in fetched]
                try:
                    conn.commit()
                except Exception:
                    pass
                return q_cols, rows, truncated, 0
            affected = int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)
            try:
                conn.commit()
            except Exception:
                pass
            return [], [], False, affected
        finally:
            cur.close()

    # =========================================================================
    # 物理级只读安全查询通道 (ReadOnly Sandbox + Hardware ReadOnly + Unconditional Rollback)
    # =========================================================================

    def execute_readonly_sql(
        self,
        connection_id: str,
        sql: str,
        *,
        limit: int = DEFAULT_READONLY_SAMPLE_LIMIT,
        max_limit_cap: int = MAX_READONLY_SAMPLE_LIMIT,
        timeout_sec: int = 12,
    ) -> QueryResult:
        """执行 100% 物理只读查询：双模态白名单校验 -> 硬件只读事务连接 -> 采样截断 -> 退出无条件 rollback."""
        profile = self.connection_hub.require_profile(connection_id)
        try:
            validated_sql = validate_readonly_sql(sql, db_type=profile.db_type)
        except ReadOnlyViolationError as vio_exc:
            self.store.add_log(
                "WARN",
                f"只读沙箱 [{profile.name}]",
                f"已硬阻断非只读或危险 SQL: {vio_exc}",
                sql_text=(sql or "")[:1500],
            )
            raise

        hard_cap = max(1, min(1000, int(max_limit_cap or MAX_READONLY_SAMPLE_LIMIT)))
        try:
            req_limit = int(limit)
        except (TypeError, ValueError):
            req_limit = DEFAULT_READONLY_SAMPLE_LIMIT
        safe_limit = max(1, min(hard_cap, req_limit))
        safe_timeout = max(2, min(25, int(timeout_sec or 12)))

        t0 = time.perf_counter()
        try:
            with self.connection_hub.readonly_session(
                connection_id, timeout_sec=min(12, safe_timeout)
            ) as conn:
                if isinstance(conn, InceptorThriftClient):
                    bounded_sql = self._build_bounded_select_sql(
                        validated_sql, profile.db_type, safe_limit + 1
                    )
                    cols_meta, raw_rows, truncated = conn.execute_query_with_meta(
                        bounded_sql, max_rows=safe_limit
                    )
                    q_cols = [
                        QueryColumn(name=c.column_name, data_type=c.data_type)
                        for c in cols_meta
                    ]
                    rows = [
                        [serialize_cell_value(cell) for cell in r]
                        for r in raw_rows
                    ]
                elif isinstance(conn, JdbcConnectionAdapter):
                    q_cols, rows, truncated = self._exec_readonly_on_jdbc(
                        conn, profile, validated_sql, safe_limit, safe_timeout
                    )
                elif profile.db_type == "SQLITE":
                    q_cols, rows, truncated = self._exec_readonly_on_sqlite(
                        conn, validated_sql, safe_limit, safe_timeout
                    )
                else:
                    q_cols, rows, truncated = self._exec_readonly_on_dbapi(
                        conn, profile, validated_sql, safe_limit, safe_timeout
                    )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.store.add_log(
                "ERROR",
                f"只读探查 [{profile.name}]",
                f"只读 SQL 执行异常 ({elapsed_ms:.1f} ms): {exc}",
                sql_text=validated_sql[:1500],
            )
            raise

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        msg = (
            f"只读采样完成：返回 {len(rows)} 行"
            + (f"（已按上限 {safe_limit} 行安全截断）" if truncated else "")
            + f"，耗时 {elapsed_ms:.1f} ms"
        )
        self.store.add_log(
            "INFO",
            f"只读探查 [{profile.name}]",
            msg,
            sql_text=validated_sql[:1500],
        )
        return QueryResult(
            connection_id=connection_id,
            sql=validated_sql,
            columns=q_cols,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            limit_applied=safe_limit,
            elapsed_ms=elapsed_ms,
            affected_rows=0,
            message=msg,
        )

    def _exec_readonly_on_sqlite(
        self,
        conn: Any,
        sql: str,
        safe_limit: int,
        timeout_sec: int,
    ) -> Tuple[List[QueryColumn], List[List[Any]], bool]:
        """SQLite 只读执行器：零 commit()，带线程级 interrupt 超时熔断."""
        timed_out = threading.Event()

        def _interrupt() -> None:
            timed_out.set()
            try:
                conn.interrupt()
            except Exception:
                pass

        bounded_sql = self._build_bounded_select_sql(sql, "SQLITE", safe_limit + 1)
        timer = threading.Timer(float(timeout_sec), _interrupt)
        timer.daemon = True
        timer.start()
        try:
            cur = conn.execute(bounded_sql)
            try:
                if not cur.description:
                    return [], [], False
                q_cols = [QueryColumn(name=str(d[0]), data_type="") for d in cur.description]
                fetched = cur.fetchmany(safe_limit + 1)
                truncated = len(fetched) > safe_limit
                if truncated:
                    fetched = fetched[:safe_limit]
                rows = [[serialize_cell_value(v) for v in r] for r in fetched]
                return q_cols, rows, truncated
            finally:
                cur.close()
        except Exception as exc:
            if timed_out.is_set():
                raise TimeoutError(f"SQLite 只读探查超过 {timeout_sec} 秒超时上限已自动中断") from exc
            raise
        finally:
            timer.cancel()

    def _exec_readonly_on_jdbc(
        self,
        conn: JdbcConnectionAdapter,
        profile: ConnectionProfile,
        sql: str,
        safe_limit: int,
        timeout_sec: int,
    ) -> Tuple[List[QueryColumn], List[List[Any]], bool]:
        """JDBC 只读执行器：零 commit()，强制 queryTimeout 与 maxRows."""
        bounded_sql = self._build_bounded_select_sql(sql, profile.db_type, safe_limit + 1)
        with conn.cursor() as cur:
            cur.execute(bounded_sql, timeout_sec=timeout_sec, max_rows=safe_limit + 1)
            if not cur.description:
                return [], [], False
            q_cols = [
                QueryColumn(name=str(d[0]), data_type=str(d[1] or ""))
                for d in cur.description
            ]
            fetched = cur.fetchmany(safe_limit + 1)
            truncated = len(fetched) > safe_limit
            if truncated:
                fetched = fetched[:safe_limit]
            rows = [[serialize_cell_value(v) for v in r] for r in fetched]
            return q_cols, rows, truncated

    def _exec_readonly_on_dbapi(
        self,
        conn: Any,
        profile: ConnectionProfile,
        sql: str,
        safe_limit: int,
        timeout_sec: int,
    ) -> Tuple[List[QueryColumn], List[List[Any]], bool]:
        """DB-API (psycopg2 / PyMySQL) 只读执行器：零 commit()，会话超时保护."""
        cur = conn.cursor()
        try:
            if profile.db_type in ("DWS", "PGSQL", "KINGBASE"):
                try:
                    cur.execute(f"SET LOCAL statement_timeout = {int(timeout_sec) * 1000};")
                except Exception:
                    pass
            bounded_sql = self._build_bounded_select_sql(sql, profile.db_type, safe_limit + 1)
            cur.execute(bounded_sql)
            if not cur.description:
                return [], [], False
            q_cols = [
                QueryColumn(
                    name=str(d[0]),
                    data_type=str(d[1].__name__ if hasattr(d[1], "__name__") else ""),
                )
                for d in cur.description
            ]
            fetched = cur.fetchmany(safe_limit + 1)
            truncated = len(fetched) > safe_limit
            if truncated:
                fetched = fetched[:safe_limit]
            rows = [[serialize_cell_value(v) for v in r] for r in fetched]
            return q_cols, rows, truncated
        finally:
            cur.close()
