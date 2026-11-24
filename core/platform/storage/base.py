"""Data.O.G 双库状态引擎底座：连接/WAL/插件表与元键注册/日志/工作区索引 (core/platform/storage/base.py)."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import weakref

from core.platform.diagnostics_hub import record_instant_metric

from core.platform.storage.schema import (
    PRIVATE_ONLY_TABLES,
    PRIVATE_SCHEMA_SQL,
    WORKSPACE_BUSINESS_TABLES,
    WORKSPACE_META_KEYS,
    _SchemaColumnMixin,
    init_workspace_schema_on_conn,
)

_SAFE_SQL_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _close_live_conns_dict(conns: Dict[str, Optional[sqlite3.Connection]]) -> None:
    """进程退出或对象销毁前安全关闭持久 SQLite 连接，消除 ResourceWarning 与文件句柄泄露."""
    seen: Set[int] = set()
    for key in list(conns.keys()):
        conn = conns.get(key)
        conns[key] = None
        if conn is not None and id(conn) not in seen:
            seen.add(id(conn))
            try:
                if getattr(conn, "in_transaction", False):
                    conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


class _TimedRLock:
    """兼容 threading.RLock 接口并统计外层锁等待与持锁耗时的轻量化计时包装器."""

    def __init__(self, owner: Any = None) -> None:
        self._rlock = threading.RLock()
        self._owner = owner
        self._local = threading.local()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        t0 = time.perf_counter()
        ok = (
            self._rlock.acquire(blocking=blocking, timeout=timeout)
            if timeout != -1
            else self._rlock.acquire(blocking=blocking)
        )
        if not ok:
            return False
        depth = int(getattr(self._local, "depth", 0))
        if depth == 0:
            wait_ms = (time.perf_counter() - t0) * 1000.0
            self._local.depth = 1
            self._local.acquired_at = time.perf_counter()
            record_instant_metric("sqlite_lock", "acquire", wait_ms, min_span_ms=1.0)
            hub = getattr(self._owner, "_diag_hub", None) if self._owner is not None else None
            if hub is not None:
                hub.record_sqlite_stat("lock", wait_ms=wait_ms)
        else:
            self._local.depth = depth + 1
        return True

    def release(self) -> None:
        depth = int(getattr(self._local, "depth", 0))
        if depth == 1:
            acq_at = getattr(self._local, "acquired_at", None)
            self._local.depth = 0
            self._local.acquired_at = None
            try:
                self._rlock.release()
            finally:
                if acq_at is not None:
                    hold_ms = (time.perf_counter() - float(acq_at)) * 1000.0
                    hub = getattr(self._owner, "_diag_hub", None) if self._owner is not None else None
                    if hub is not None:
                        hub.record_sqlite_stat("lock", hold_ms=hold_ms)
            return
        if depth > 1:
            self._local.depth = depth - 1
        self._rlock.release()

    def __enter__(self) -> "_TimedRLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


class _BaseStateStore(_SchemaColumnMixin):
    """三层分离 SQLite 底座核心：双库连接、插件表注册、元键值分流与工作区登记."""

    def __init__(
        self,
        db_path: str,
        *,
        workspace_db_path: Optional[str] = None,
        clock_anchor_path: Optional[str] = None,
    ) -> None:
        self.private_db_path = os.path.abspath(db_path)
        self.db_path = self.private_db_path
        self.workspace_db_path = (
            os.path.abspath(workspace_db_path) if workspace_db_path else self.private_db_path
        )
        self.is_split_storage = self.private_db_path != self.workspace_db_path
        self.clock_anchor_path = os.path.abspath(clock_anchor_path) if clock_anchor_path else None

        os.makedirs(os.path.dirname(self.private_db_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.workspace_db_path), exist_ok=True)

        self._diag_hub: Any = None
        self._lock = _TimedRLock(owner=self)
        self._live_conns: Dict[str, Optional[sqlite3.Connection]] = {
            "private": None,
            "workspace": None,
        }
        self._conn_depth: Dict[str, int] = {"private": 0, "workspace": 0}
        self._init_open_stats: Dict[str, Dict[str, float]] = {
            "private": {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "reuses": 0},
            "workspace": {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "reuses": 0},
        }
        self._finalizer = weakref.finalize(self, _close_live_conns_dict, self._live_conns)
        self._registered_schemas: Dict[str, List[Tuple[str, str, List[Tuple[str, str]]]]] = {
            "private": [],
            "workspace": [],
        }
        self._plugin_private_tables: Set[str] = set()
        self._plugin_workspace_tables: Set[str] = set()
        self._extra_workspace_meta_keys: Set[str] = set()

        self._init_private_db()
        self._init_workspace_db()
        self._migrate_legacy_db_file_if_needed()
        self._migrate_legacy_connection_profiles_in_db()
        if not self.is_split_storage or not self.get_meta("active_workspace_id", ""):
            self._migrate_legacy_connections_if_needed()

    @staticmethod
    def _connect_sqlite(path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000;")
        try:
            conn.execute("PRAGMA temp_store = MEMORY;")
            conn.execute("PRAGMA cache_size = -16000;")
        except Exception:
            pass
        return conn

    def _drop_persistent_conn(
        self, eff_kind: str, *, checkpoint: bool = False, checkpoint_mode: str = "PASSIVE"
    ) -> None:
        with self._lock:
            conn = self._live_conns.get(eff_kind)
            self._live_conns[eff_kind] = None
            self._conn_depth[eff_kind] = 0
            if conn is None:
                return
            mode = (
                checkpoint_mode.upper()
                if str(checkpoint_mode or "").upper() in ("PASSIVE", "FULL", "RESTART", "TRUNCATE")
                else "PASSIVE"
            )
            try:
                if getattr(conn, "in_transaction", False):
                    conn.rollback()
                if checkpoint:
                    conn.execute(f"PRAGMA wal_checkpoint({mode});")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _acquire_persistent_conn(
        self, kind: str, eff_kind: str
    ) -> Tuple[sqlite3.Connection, float, bool]:
        existing = self._live_conns.get(eff_kind)
        hub = getattr(self, "_diag_hub", None)
        if existing is not None:
            if hub is not None:
                hub.record_sqlite_stat(f"{kind}_reuse")
            else:
                st = self._init_open_stats[kind]
                if kind == "workspace" and st["count"] == 0 and eff_kind != kind:
                    st["count"] = 1
                st["reuses"] += 1
            return existing, 0.0, True

        target_path = (
            self.private_db_path if eff_kind == "private" else self.workspace_db_path
        )
        t0 = time.perf_counter()
        conn = self._connect_sqlite(target_path)
        open_ms = (time.perf_counter() - t0) * 1000.0
        self._live_conns[eff_kind] = conn
        if hub is not None:
            hub.record_sqlite_stat(f"{kind}_open", open_ms=open_ms)
        else:
            st = self._init_open_stats[kind]
            st["count"] += 1
            st["total_ms"] += open_ms
            if open_ms > st["max_ms"]:
                st["max_ms"] = open_ms
        if open_ms >= 2.0:
            record_instant_metric(f"sqlite_{kind}", "open", open_ms, min_span_ms=2.0)
        return conn, open_ms, False

    @contextlib.contextmanager
    def _managed_conn(self, kind: str, *, autocommit: bool = False):
        """复用持久 SQLite 连接并在外层临界区退出时收敛未决事务，彻底消除高频开关句柄与 EDR 扫描开销."""
        t0 = time.perf_counter()
        with self._lock:
            eff_kind = (
                "private"
                if (kind == "private" or not self.is_split_storage)
                else "workspace"
            )
            conn, open_ms, reused = self._acquire_persistent_conn(kind, eff_kind)
            self._conn_depth[eff_kind] = int(self._conn_depth.get(eff_kind, 0)) + 1
            is_outer = self._conn_depth[eff_kind] == 1
            try:
                yield conn
                if is_outer and getattr(conn, "in_transaction", False):
                    if autocommit:
                        conn.commit()
                    else:
                        conn.rollback()
            except Exception as exc:
                if is_outer and getattr(conn, "in_transaction", False):
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                if isinstance(exc, (sqlite3.DatabaseError, sqlite3.ProgrammingError)):
                    self._drop_persistent_conn(eff_kind, checkpoint=False)
                raise
            finally:
                cur_depth = int(self._conn_depth.get(eff_kind, 0))
                self._conn_depth[eff_kind] = max(0, cur_depth - 1)
                total_ms = (time.perf_counter() - t0) * 1000.0
                record_instant_metric(
                    f"sqlite_{kind}",
                    "session",
                    total_ms,
                    {"open_ms": round(open_ms, 2), "reused": reused},
                    min_span_ms=5.0,
                )

    @contextlib.contextmanager
    def _get_private_conn(self):
        with self._managed_conn("private", autocommit=False) as conn:
            yield conn

    @contextlib.contextmanager
    def _get_workspace_conn(self):
        with self._managed_conn("workspace", autocommit=False) as conn:
            yield conn

    @contextlib.contextmanager
    def _get_conn(self):
        """默认指向当前活动工作区数据库（在单库兼容模式下与私有库为同一文件）."""
        with self._managed_conn("workspace", autocommit=False) as conn:
            yield conn

    @contextlib.contextmanager
    def _get_both_conns(self):
        """同时获取 (private_conn, workspace_conn)；单库兼容模式下复用同一连接避免同文件双写锁."""
        if self.is_split_storage:
            with self._get_private_conn() as p_conn, self._get_workspace_conn() as w_conn:
                yield p_conn, w_conn
        else:
            with self._get_private_conn() as conn:
                yield conn, conn

    @staticmethod
    def _now_str() -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _init_private_db(self) -> None:
        with self._lock, self._get_private_conn() as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception:
                pass
            conn.executescript(PRIVATE_SCHEMA_SQL)
            self._ensure_private_schema_columns(conn)
            conn.commit()

    def _init_workspace_db(self) -> None:
        os.makedirs(os.path.dirname(self.workspace_db_path), exist_ok=True)
        with self._lock, self._get_workspace_conn() as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception:
                pass
            init_workspace_schema_on_conn(conn)
            for tbl_name, create_sql, exp_cols in self._registered_schemas.get("workspace", []):
                if create_sql.strip():
                    conn.executescript(create_sql)
                self._ensure_columns_for_table(conn, tbl_name, exp_cols)
            conn.commit()

    def checkpoint_workspace(self) -> None:
        """在不重开连接的前提下对当前工作区 WAL 执行 PASSIVE checkpoint."""
        with self._lock:
            eff_kind = "workspace" if self.is_split_storage else "private"
            conn = self._live_conns.get(eff_kind)
            if conn is None:
                return
            try:
                if not getattr(conn, "in_transaction", False):
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            except Exception:
                pass

    def close(self) -> None:
        """优雅关闭私有库与工作区库持久连接（执行 TRUNCATE 刷盘并释放句柄）."""
        with self._lock:
            self._drop_persistent_conn("workspace", checkpoint=True, checkpoint_mode="TRUNCATE")
            self._drop_persistent_conn("private", checkpoint=True, checkpoint_mode="TRUNCATE")

    def switch_workspace_db(self, new_workspace_db_path: str) -> None:
        """原子热切换当前活动的 Layer 3 团队共享工作区 SQLite 文件（不改动私有保险箱）."""
        target = os.path.abspath(new_workspace_db_path)
        with self._lock:
            if self.is_split_storage:
                self._drop_persistent_conn("workspace", checkpoint=True)
            else:
                self.checkpoint_workspace()
            self.workspace_db_path = target
            self.is_split_storage = self.private_db_path != self.workspace_db_path
            self._init_workspace_db()

    def register_table_schema(
        self,
        table_name: str,
        create_sql: str,
        expected_columns: List[Tuple[str, str]],
        *,
        scope: str = "workspace",
    ) -> None:
        """供业务插件模块幂等注册私有/工作区 SQLite 表结构并自动演进补齐新增列."""
        clean_table = str(table_name or "").strip()
        if not _SAFE_SQL_IDENT_RE.match(clean_table):
            raise ValueError(f"非法的插件表名: {table_name!r}")
        norm_scope = "private" if str(scope or "").strip().lower() == "private" else "workspace"
        with self._lock:
            if norm_scope == "workspace" and clean_table in self.get_private_table_names():
                raise ValueError(f"私有保险箱表 [{clean_table}] 严禁注册为共享工作区表")
            bucket = self._registered_schemas[norm_scope]
            if norm_scope == "private":
                self._plugin_private_tables.add(clean_table)
                self._plugin_workspace_tables.discard(clean_table)
            else:
                self._plugin_workspace_tables.add(clean_table)
            existing_idx = next((i for i, t in enumerate(bucket) if t[0] == clean_table), None)
            if existing_idx is None:
                bucket.append((clean_table, create_sql, list(expected_columns)))
            else:
                bucket[existing_idx] = (clean_table, create_sql, list(expected_columns))
            cm = self._get_private_conn() if norm_scope == "private" else self._get_workspace_conn()
            with cm as conn:
                if create_sql.strip():
                    conn.executescript(create_sql)
                self._ensure_columns_for_table(conn, clean_table, expected_columns)
                conn.commit()

    @contextlib.contextmanager
    def module_db(self, scope: str = "workspace"):
        """供业务插件模块执行参数化 SQLite 读写事务的线程安全上下文."""
        norm_scope = "private" if str(scope or "").strip().lower() == "private" else "workspace"
        with self._managed_conn(norm_scope, autocommit=True) as conn:
            yield conn


    def register_private_tables(self, table_names: Iterable[str]) -> None:
        """声明仅限本地私有保险箱存放的插件表，防止被工作区导出或合并误读."""
        with self._lock:
            for name in table_names:
                clean = str(name or "").strip()
                if clean and _SAFE_SQL_IDENT_RE.match(clean):
                    self._plugin_private_tables.add(clean)
                    self._plugin_workspace_tables.discard(clean)

    def register_workspace_tables(self, table_names: Iterable[str]) -> None:
        """声明归属团队共享工作区的插件表白名单."""
        with self._lock:
            private_names = self.get_private_table_names()
            for name in table_names:
                clean = str(name or "").strip()
                if not clean:
                    continue
                if not _SAFE_SQL_IDENT_RE.match(clean):
                    raise ValueError(f"非法的插件工作区表名: {name!r}")
                if clean in private_names:
                    raise ValueError(f"私有保险箱表 [{clean}] 严禁注册为共享工作区表")
                self._plugin_workspace_tables.add(clean)

    def register_workspace_meta_keys(self, keys: Iterable[str]) -> None:
        """动态注册插件归属的 workspace_meta 业务键，实现免改底座接入团队共享工作区."""
        with self._lock:
            for key in keys:
                clean = str(key or "").strip()
                if clean:
                    self._extra_workspace_meta_keys.add(clean)

    def _is_workspace_meta_key(self, key: str) -> bool:
        return key in WORKSPACE_META_KEYS or key in self._extra_workspace_meta_keys

    def get_workspace_meta_keys(self) -> Set[str]:
        with self._lock:
            return set(WORKSPACE_META_KEYS) | set(self._extra_workspace_meta_keys)

    def get_private_table_names(self) -> Set[str]:
        with self._lock:
            return set(PRIVATE_ONLY_TABLES) | set(self._plugin_private_tables)

    def get_registered_plugin_workspace_specs(
        self,
    ) -> List[Tuple[str, str, List[Tuple[str, str]]]]:
        """返回通过 register_table_schema(scope=workspace) 注册的自定义插件共享表清单（严格排除私有表与核心内建表）."""
        builtin = set(WORKSPACE_BUSINESS_TABLES)
        private_set = self.get_private_table_names()
        with self._lock:
            return [
                (tbl, ddl, list(cols))
                for tbl, ddl, cols in self._registered_schemas.get("workspace", [])
                if tbl not in builtin and tbl not in private_set and _SAFE_SQL_IDENT_RE.match(tbl)
            ]

    # =========================================================================
    # 基础键值配置（按白名单自动分流至私有 app_meta 或工作区 workspace_meta）与日志中心
    # =========================================================================

    def save_meta(self, key: str, value: Any) -> None:
        val_str = json.dumps(value, ensure_ascii=False)
        if self._is_workspace_meta_key(key):
            with self._lock, self._get_workspace_conn() as conn:
                conn.execute(
                    "INSERT INTO workspace_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, val_str),
                )
                conn.commit()
            return
        with self._lock, self._get_private_conn() as conn:
            conn.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, val_str),
            )
            conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        if self._is_workspace_meta_key(key):
            with self._lock, self._get_workspace_conn() as conn:
                row = conn.execute(
                    "SELECT value FROM workspace_meta WHERE key = ?", (key,)
                ).fetchone()
                if row:
                    try:
                        return json.loads(row["value"])
                    except Exception:
                        return default
            # 单库兼容回退：历史文件中可能存于 app_meta
            if not self.is_split_storage:
                with self._lock, self._get_private_conn() as conn:
                    row2 = conn.execute(
                        "SELECT value FROM app_meta WHERE key = ?", (key,)
                    ).fetchone()
                    if row2:
                        try:
                            return json.loads(row2["value"])
                        except Exception:
                            return default
            return default

        with self._lock, self._get_private_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return default

    def get_meta_batch(self, keys: Iterable[str]) -> Dict[str, Any]:
        """单次事务/连接按路由批量读取 app_meta 与 workspace_meta 键值."""
        uniq_keys = list(dict.fromkeys(str(k) for k in keys if str(k)))
        if not uniq_keys:
            return {}
        ws_keys = [k for k in uniq_keys if self._is_workspace_meta_key(k)]
        priv_keys = [k for k in uniq_keys if not self._is_workspace_meta_key(k)]
        out: Dict[str, Any] = {}
        with self._lock:
            if priv_keys:
                ph = ",".join("?" for _ in priv_keys)
                with self._get_private_conn() as pconn:
                    for row in pconn.execute(
                        f"SELECT key, value FROM app_meta WHERE key IN ({ph})",
                        priv_keys,
                    ).fetchall():
                        try:
                            out[str(row["key"])] = json.loads(row["value"])
                        except Exception:
                            pass
            if ws_keys:
                ph = ",".join("?" for _ in ws_keys)
                with self._get_workspace_conn() as wconn:
                    for row in wconn.execute(
                        f"SELECT key, value FROM workspace_meta WHERE key IN ({ph})",
                        ws_keys,
                    ).fetchall():
                        try:
                            out[str(row["key"])] = json.loads(row["value"])
                        except Exception:
                            pass
                missing_ws = [k for k in ws_keys if k not in out]
                if missing_ws and not self.is_split_storage:
                    ph2 = ",".join("?" for _ in missing_ws)
                    with self._get_private_conn() as pconn:
                        for row in pconn.execute(
                            f"SELECT key, value FROM app_meta WHERE key IN ({ph2})",
                            missing_ws,
                        ).fetchall():
                            try:
                                out[str(row["key"])] = json.loads(row["value"])
                            except Exception:
                                pass
        return out

    def add_log(self, level: str, target: str, message: str, sql_text: str = "") -> None:
        now = self._now_str()
        with self._lock, self._get_private_conn() as conn:
            conn.execute(
                """
                INSERT INTO execution_logs (created_at, level, target, message, sql_text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now, level.upper(), target, message, sql_text),
            )
            conn.execute(
                """
                DELETE FROM execution_logs
                WHERE id NOT IN (
                    SELECT id FROM execution_logs ORDER BY id DESC LIMIT 1000
                )
                """
            )
            conn.commit()

    def list_logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock, self._get_private_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_logs(self) -> None:
        with self._lock, self._get_private_conn() as conn:
            conn.execute("DELETE FROM execution_logs")
            conn.commit()

    # =========================================================================
    # 本地托管工作区注册表 (Private Vault `workspaces`)
    # =========================================================================

    def list_workspace_records(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_private_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workspaces ORDER BY is_default DESC, updated_at DESC, created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_workspace_record(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        if not workspace_id:
            return None
        with self._lock, self._get_private_conn() as conn:
            row = conn.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            return dict(row) if row else None

    def upsert_workspace_record(
        self,
        *,
        workspace_id: str,
        name: str,
        file_name: str,
        description: str = "",
        is_default: bool = False,
        created_at: str = "",
    ) -> Dict[str, Any]:
        now = self._now_str()
        with self._lock, self._get_private_conn() as conn:
            existing = conn.execute(
                "SELECT created_at FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            eff_created = created_at or (existing["created_at"] if existing else now)
            conn.execute(
                """
                INSERT INTO workspaces (
                    workspace_id, name, description, file_name, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    file_name = excluded.file_name,
                    is_default = excluded.is_default,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    name,
                    description,
                    file_name,
                    1 if is_default else 0,
                    eff_created,
                    now,
                ),
            )
            conn.commit()
        return self.get_workspace_record(workspace_id) or {}

    def delete_workspace_record(self, workspace_id: str) -> bool:
        with self._lock, self._get_private_conn() as conn:
            cur = conn.execute(
                "DELETE FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            )
            conn.commit()
            return cur.rowcount > 0

