"""SQLite 本地状态持久化存储模块.

负责保存：
1. 贯标清单导入批次与每条字段转换任务的持久化状态
2. 与华为 DWS 数据库联动比对后的实时状态、字段序号 (attnum)、当前库内属性
3. 每次执行 SQL 的操作日志、执行耗时与报错堆栈
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple


class StateStore:
    """线程安全的本地 SQLite 状态管理器（启用 WAL 模式与批量事务优化）."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextlib.contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 10000;")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._get_conn() as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception:
                pass
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS migration_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    row_index INTEGER NOT NULL,
                    schema_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    old_col_name TEXT NOT NULL,
                    old_col_type TEXT NOT NULL DEFAULT '',
                    old_col_comment TEXT NOT NULL DEFAULT '',
                    new_col_name TEXT NOT NULL,
                    new_col_type TEXT NOT NULL,
                    new_col_comment TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'UNCHECKED',
                    strategy TEXT NOT NULL DEFAULT 'DIRECT_ALTER',
                    ordinal_position INTEGER,
                    total_columns INTEGER,
                    db_current_col_name TEXT DEFAULT '',
                    db_current_type TEXT DEFAULT '',
                    db_current_comment TEXT DEFAULT '',
                    is_distribute_key INTEGER DEFAULT 0,
                    table_orientation TEXT DEFAULT '',
                    status_detail TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    generated_sql TEXT DEFAULT '',
                    executed_at TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    target TEXT NOT NULL,
                    message TEXT NOT NULL,
                    sql_text TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cross_table_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    std_table_key TEXT NOT NULL UNIQUE,
                    std_schema_name TEXT NOT NULL DEFAULT 'public',
                    std_table_name TEXT NOT NULL,
                    std_table_comment TEXT NOT NULL DEFAULT '',
                    source_schema TEXT NOT NULL DEFAULT '',
                    source_table TEXT NOT NULL DEFAULT '',
                    target_schema TEXT NOT NULL DEFAULT '',
                    target_table TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cross_column_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    std_table_key TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    orig_col_name TEXT NOT NULL DEFAULT '',
                    orig_col_type TEXT NOT NULL DEFAULT '',
                    orig_col_comment TEXT NOT NULL DEFAULT '',
                    std_col_name TEXT NOT NULL,
                    std_col_type TEXT NOT NULL DEFAULT '',
                    std_col_comment TEXT NOT NULL DEFAULT '',
                    bound_source_col TEXT NOT NULL DEFAULT '',
                    bound_target_col TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_schema_columns(conn)
            conn.commit()

    @staticmethod
    def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
        """幂等补齐表结构列定义，保障跨 Git 分支切换与版本回退时本地 SQLite 状态库前后向兼容."""
        expected_columns: Dict[str, List[Tuple[str, str]]] = {
            "migration_items": [
                ("old_col_type", "TEXT NOT NULL DEFAULT ''"),
                ("old_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("new_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("status", "TEXT NOT NULL DEFAULT 'UNCHECKED'"),
                ("strategy", "TEXT NOT NULL DEFAULT 'DIRECT_ALTER'"),
                ("ordinal_position", "INTEGER"),
                ("total_columns", "INTEGER"),
                ("db_current_col_name", "TEXT DEFAULT ''"),
                ("db_current_type", "TEXT DEFAULT ''"),
                ("db_current_comment", "TEXT DEFAULT ''"),
                ("is_distribute_key", "INTEGER DEFAULT 0"),
                ("table_orientation", "TEXT DEFAULT ''"),
                ("status_detail", "TEXT DEFAULT ''"),
                ("error_message", "TEXT DEFAULT ''"),
                ("generated_sql", "TEXT DEFAULT ''"),
                ("executed_at", "TEXT DEFAULT ''"),
            ],
            "execution_logs": [
                ("sql_text", "TEXT DEFAULT ''"),
            ],
            "cross_table_bindings": [
                ("std_schema_name", "TEXT NOT NULL DEFAULT 'public'"),
                ("std_table_comment", "TEXT NOT NULL DEFAULT ''"),
                ("source_schema", "TEXT NOT NULL DEFAULT ''"),
                ("source_table", "TEXT NOT NULL DEFAULT ''"),
                ("target_schema", "TEXT NOT NULL DEFAULT ''"),
                ("target_table", "TEXT NOT NULL DEFAULT ''"),
            ],
            "cross_column_bindings": [
                ("orig_col_name", "TEXT NOT NULL DEFAULT ''"),
                ("orig_col_type", "TEXT NOT NULL DEFAULT ''"),
                ("orig_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("std_col_type", "TEXT NOT NULL DEFAULT ''"),
                ("std_col_comment", "TEXT NOT NULL DEFAULT ''"),
                ("bound_source_col", "TEXT NOT NULL DEFAULT ''"),
                ("bound_target_col", "TEXT NOT NULL DEFAULT ''"),
            ],
        }
        for table_name, col_defs in expected_columns.items():
            existing = {
                row["name"].lower()
                for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            if not existing:
                continue
            for col_name, col_ddl in col_defs:
                if col_name.lower() not in existing:
                    conn.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_ddl}"
                    )

    @staticmethod
    def _now_str() -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def save_meta(self, key: str, value: Any) -> None:
        val_str = json.dumps(value, ensure_ascii=False)
        with self._lock, self._get_conn() as conn:
            conn.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, val_str),
            )
            conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return default

    def replace_all_items(
        self,
        items: List[Dict[str, Any]],
        source_filename: str,
        preserve_history: bool = True,
    ) -> List[Dict[str, Any]]:
        """导入新的 Excel 清单或载入目标库表结构时替换清单记录（单事务批量写入）."""
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            history_map: Dict[str, Dict[str, Any]] = {}
            if preserve_history:
                existing_rows = conn.execute("SELECT * FROM migration_items").fetchall()
                for r in existing_rows:
                    key = f"{r['schema_name']}.{r['table_name']}.{r['old_col_name']}->{r['new_col_name']}".lower()
                    history_map[key] = dict(r)

            conn.execute("DELETE FROM migration_items")
            rows_to_insert = []
            quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
            for idx, item in enumerate(items, start=1):
                schema_name = str(item.get("schema_name", "")).strip(quote_chars) or "public"
                table_name = str(item.get("table_name", "")).strip(quote_chars)
                old_col_name = str(item.get("old_col_name", "")).strip(quote_chars)
                old_col_type = str(item.get("old_col_type", "")).strip()
                old_col_comment = str(item.get("old_col_comment", "")).strip()
                new_col_name = str(item.get("new_col_name", "")).strip(quote_chars) or old_col_name
                new_col_type = str(item.get("new_col_type", "")).strip() or old_col_type
                new_col_comment = str(item.get("new_col_comment", "")).strip()

                key = f"{schema_name}.{table_name}.{old_col_name}->{new_col_name}".lower()
                prev = history_map.get(key)

                status = item.get("status") or (prev["status"] if prev else "UNCHECKED")
                status_detail = item.get("status_detail") or (
                    prev["status_detail"]
                    if prev
                    else "已导入清单，等待连接数据库同步元数据状态"
                )
                error_message = prev["error_message"] if prev else ""
                executed_at = prev["executed_at"] if prev else ""
                ordinal_pos = item.get("ordinal_position") or (
                    prev["ordinal_position"] if prev else None
                )
                total_cols = item.get("total_columns") or (
                    prev["total_columns"] if prev else None
                )
                db_col = item.get("db_current_col_name") or (
                    prev["db_current_col_name"] if prev else ""
                )
                db_type = item.get("db_current_type") or (
                    prev["db_current_type"] if prev else ""
                )
                db_comment = item.get("db_current_comment") or (
                    prev["db_current_comment"] if prev else ""
                )
                is_dist = (
                    1
                    if item.get("is_distribute_key")
                    else (prev["is_distribute_key"] if prev else 0)
                )
                orientation = item.get("table_orientation") or (
                    prev["table_orientation"] if prev else ""
                )

                rows_to_insert.append(
                    (
                        item.get("row_index", idx),
                        schema_name,
                        table_name,
                        old_col_name,
                        old_col_type,
                        old_col_comment,
                        new_col_name,
                        new_col_type,
                        new_col_comment,
                        status,
                        "DIRECT_ALTER",
                        ordinal_pos,
                        total_cols,
                        db_col,
                        db_type,
                        db_comment,
                        is_dist,
                        orientation,
                        status_detail,
                        error_message,
                        executed_at,
                        now,
                    )
                )

            conn.executemany(
                """
                INSERT INTO migration_items (
                    row_index, schema_name, table_name,
                    old_col_name, old_col_type, old_col_comment,
                    new_col_name, new_col_type, new_col_comment,
                    status, strategy, ordinal_position, total_columns,
                    db_current_col_name, db_current_type, db_current_comment,
                    is_distribute_key, table_orientation,
                    status_detail, error_message, executed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
            conn.commit()
        self.save_meta("current_excel_name", source_filename)
        self.save_meta("imported_at", now)
        self.add_log(
            "INFO",
            "清单载入",
            f"成功载入清单 [{source_filename}]，共 {len(items)} 条字段规则。",
        )
        return self.list_items()

    def list_items(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM migration_items ORDER BY row_index ASC, id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM migration_items WHERE id = ?", (item_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_item_sync(
        self,
        item_id: int,
        *,
        status: str,
        status_detail: str,
        schema_name: Optional[str] = None,
        table_name: Optional[str] = None,
        ordinal_position: Optional[int] = None,
        total_columns: Optional[int] = None,
        db_current_col_name: str = "",
        db_current_type: str = "",
        db_current_comment: str = "",
        is_distribute_key: bool = False,
        table_orientation: str = "",
        generated_sql: str = "",
        clear_error: bool = False,
    ) -> None:
        self.batch_update_items_sync(
            [
                {
                    "item_id": item_id,
                    "status": status,
                    "status_detail": status_detail,
                    "schema_name": schema_name,
                    "table_name": table_name,
                    "ordinal_position": ordinal_position,
                    "total_columns": total_columns,
                    "db_current_col_name": db_current_col_name,
                    "db_current_type": db_current_type,
                    "db_current_comment": db_current_comment,
                    "is_distribute_key": is_distribute_key,
                    "table_orientation": table_orientation,
                    "generated_sql": generated_sql,
                    "clear_error": clear_error,
                }
            ]
        )

    def batch_update_items_sync(self, updates: List[Dict[str, Any]]) -> None:
        """在单个 SQLite 事务内批量更新所有字段的元数据比对结果（避免逐行 fsync 卡顿）."""
        if not updates:
            return
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            for u in updates:
                item_id = u["item_id"]
                status = u["status"]
                status_detail = u["status_detail"]
                ordinal_position = u.get("ordinal_position")
                total_columns = u.get("total_columns")
                db_current_col_name = u.get("db_current_col_name", "")
                db_current_type = u.get("db_current_type", "")
                db_current_comment = u.get("db_current_comment", "")
                is_dist = 1 if u.get("is_distribute_key") else 0
                table_orientation = u.get("table_orientation", "")
                generated_sql = u.get("generated_sql", "")
                clear_error = bool(u.get("clear_error", False))
                schema_name = u.get("schema_name")
                table_name = u.get("table_name")

                if schema_name and table_name:
                    conn.execute(
                        """
                        UPDATE migration_items
                        SET schema_name = ?,
                            table_name = ?,
                            status = ?,
                            status_detail = ?,
                            ordinal_position = ?,
                            total_columns = ?,
                            db_current_col_name = ?,
                            db_current_type = ?,
                            db_current_comment = ?,
                            is_distribute_key = ?,
                            table_orientation = ?,
                            generated_sql = ?,
                            error_message = CASE WHEN ? THEN '' ELSE error_message END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            schema_name,
                            table_name,
                            status,
                            status_detail,
                            ordinal_position,
                            total_columns,
                            db_current_col_name,
                            db_current_type,
                            db_current_comment,
                            is_dist,
                            table_orientation,
                            generated_sql,
                            1 if clear_error else 0,
                            now,
                            item_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE migration_items
                        SET status = ?,
                            status_detail = ?,
                            ordinal_position = ?,
                            total_columns = ?,
                            db_current_col_name = ?,
                            db_current_type = ?,
                            db_current_comment = ?,
                            is_distribute_key = ?,
                            table_orientation = ?,
                            generated_sql = ?,
                            error_message = CASE WHEN ? THEN '' ELSE error_message END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            status_detail,
                            ordinal_position,
                            total_columns,
                            db_current_col_name,
                            db_current_type,
                            db_current_comment,
                            is_dist,
                            table_orientation,
                            generated_sql,
                            1 if clear_error else 0,
                            now,
                            item_id,
                        ),
                    )
            conn.commit()

    def update_item_execution(
        self,
        item_id: int,
        *,
        status: str,
        strategy: str,
        status_detail: str,
        error_message: str = "",
        generated_sql: str = "",
        ordinal_position: Optional[int] = None,
        db_current_col_name: Optional[str] = None,
        db_current_type: Optional[str] = None,
        db_current_comment: Optional[str] = None,
    ) -> None:
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            current = conn.execute(
                "SELECT * FROM migration_items WHERE id = ?", (item_id,)
            ).fetchone()
            if not current:
                return
            conn.execute(
                """
                UPDATE migration_items
                SET status = ?,
                    strategy = ?,
                    status_detail = ?,
                    error_message = ?,
                    generated_sql = ?,
                    ordinal_position = ?,
                    db_current_col_name = ?,
                    db_current_type = ?,
                    db_current_comment = ?,
                    executed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    strategy,
                    status_detail,
                    error_message,
                    generated_sql or current["generated_sql"],
                    ordinal_position if ordinal_position is not None else current["ordinal_position"],
                    db_current_col_name if db_current_col_name is not None else current["db_current_col_name"],
                    db_current_type if db_current_type is not None else current["db_current_type"],
                    db_current_comment if db_current_comment is not None else current["db_current_comment"],
                    now,
                    now,
                    item_id,
                ),
            )
            conn.commit()

    def add_log(self, level: str, target: str, message: str, sql_text: str = "") -> None:
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
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
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_items(self) -> None:
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM migration_items")
            conn.commit()
        self.save_meta("current_excel_name", "未导入")
        self.save_meta("imported_at", "")

    def clear_logs(self) -> None:
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM execution_logs")
            conn.commit()

    def get_summary(self) -> Dict[str, int]:
        items = self.list_items()
        summary = {
            "total": len(items),
            "completed": 0,
            "pending": 0,
            "partial": 0,
            "failed": 0,
            "missing": 0,
            "unchecked": 0,
        }
        for item in items:
            st = item["status"]
            if st == "COMPLETED":
                summary["completed"] += 1
            elif st == "PENDING":
                summary["pending"] += 1
            elif st == "PARTIAL":
                summary["partial"] += 1
            elif st == "FAILED":
                summary["failed"] += 1
            elif st in ("MISSING_TABLE", "MISSING_COLUMN"):
                summary["missing"] += 1
            else:
                summary["unchecked"] += 1
        return summary

    # =========================================================================
    # 跨环境三栏数标比对中心 (Source DB × Data Standard × Target DB) 持久化方法
    # =========================================================================

    def replace_cross_standards(
        self,
        items: List[Dict[str, Any]],
        source_filename: str,
        preserve_bindings: bool = True,
    ) -> List[Dict[str, Any]]:
        """导入或同步数据标准/落标清单至三栏比对中心，自动保留已有手工绑定关系."""
        now = self._now_str()
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        with self._lock, self._get_conn() as conn:
            prev_tables: Dict[str, Dict[str, Any]] = {}
            prev_cols: Dict[str, Dict[str, Any]] = {}
            if preserve_bindings:
                for r in conn.execute("SELECT * FROM cross_table_bindings").fetchall():
                    prev_tables[r["std_table_key"].lower()] = dict(r)
                for r in conn.execute("SELECT * FROM cross_column_bindings").fetchall():
                    k = f"{r['std_table_key']}::{r['orig_col_name']}->{r['std_col_name']}".lower()
                    prev_cols[k] = dict(r)
                    k2 = f"{r['std_table_key']}::{r['std_col_name']}".lower()
                    prev_cols.setdefault(k2, dict(r))

            conn.execute("DELETE FROM cross_table_bindings")
            conn.execute("DELETE FROM cross_column_bindings")

            table_rows_map: Dict[str, Tuple[str, str, str, str, str, str, str, str]] = {}
            col_rows_to_insert = []
            per_table_idx: Dict[str, int] = {}

            for item in items:
                schema_name = str(item.get("schema_name") or item.get("std_schema_name") or "").strip(quote_chars) or "public"
                table_name = str(item.get("table_name") or item.get("std_table_name") or "").strip(quote_chars)
                if not table_name:
                    continue
                t_key = table_name.lower()

                old_col_name = str(item.get("old_col_name") or item.get("orig_col_name") or "").strip(quote_chars)
                old_col_type = str(item.get("old_col_type") or item.get("orig_col_type") or "").strip()
                old_col_comment = str(item.get("old_col_comment") or item.get("orig_col_comment") or "").strip()
                std_col_name = str(item.get("new_col_name") or item.get("std_col_name") or "").strip(quote_chars) or old_col_name
                std_col_type = str(item.get("new_col_type") or item.get("std_col_type") or "").strip() or old_col_type
                std_col_comment = str(item.get("new_col_comment") or item.get("std_col_comment") or "").strip() or old_col_comment

                if t_key not in table_rows_map:
                    prev_t = prev_tables.get(t_key)
                    src_schema = str(item.get("source_schema") or (prev_t["source_schema"] if prev_t else schema_name)).strip(quote_chars)
                    src_table = str(item.get("source_table") or (prev_t["source_table"] if prev_t else table_name)).strip(quote_chars)
                    tgt_schema = str(item.get("target_schema") or (prev_t["target_schema"] if prev_t else schema_name)).strip(quote_chars)
                    tgt_table = str(item.get("target_table") or (prev_t["target_table"] if prev_t else table_name)).strip(quote_chars)
                    t_comment = str(item.get("std_table_comment") or (prev_t["std_table_comment"] if prev_t else "")).strip()
                    table_rows_map[t_key] = (
                        t_key,
                        schema_name,
                        table_name,
                        t_comment,
                        src_schema,
                        src_table,
                        tgt_schema,
                        tgt_table,
                    )

                per_table_idx[t_key] = per_table_idx.get(t_key, 0) + 1
                row_idx = int(item.get("row_index") or per_table_idx[t_key])

                c_key1 = f"{t_key}::{old_col_name}->{std_col_name}".lower()
                c_key2 = f"{t_key}::{std_col_name}".lower()
                prev_c = prev_cols.get(c_key1) or prev_cols.get(c_key2)

                bound_src = str(
                    item.get("bound_source_col")
                    or (prev_c["bound_source_col"] if prev_c else "")
                    or old_col_name
                    or std_col_name
                ).strip(quote_chars)
                bound_tgt = str(
                    item.get("bound_target_col")
                    or (prev_c["bound_target_col"] if prev_c else "")
                    or std_col_name
                    or old_col_name
                ).strip(quote_chars)

                col_rows_to_insert.append(
                    (
                        t_key,
                        row_idx,
                        old_col_name,
                        old_col_type,
                        old_col_comment,
                        std_col_name,
                        std_col_type,
                        std_col_comment,
                        bound_src,
                        bound_tgt,
                        now,
                    )
                )

            conn.executemany(
                """
                INSERT INTO cross_table_bindings (
                    std_table_key, std_schema_name, std_table_name, std_table_comment,
                    source_schema, source_table, target_schema, target_table, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(*vals, now) for vals in table_rows_map.values()],
            )
            conn.executemany(
                """
                INSERT INTO cross_column_bindings (
                    std_table_key, row_index,
                    orig_col_name, orig_col_type, orig_col_comment,
                    std_col_name, std_col_type, std_col_comment,
                    bound_source_col, bound_target_col, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                col_rows_to_insert,
            )
            conn.commit()

        self.save_meta("cross_standard_filename", source_filename)
        self.save_meta("cross_standard_imported_at", now)
        return self.list_cross_tables()

    def list_cross_tables(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            t_rows = conn.execute(
                """
                SELECT
                    t.*,
                    COUNT(c.id) AS column_count
                FROM cross_table_bindings t
                LEFT JOIN cross_column_bindings c ON c.std_table_key = t.std_table_key
                GROUP BY t.id
                ORDER BY t.id ASC
                """
            ).fetchall()
            return [dict(r) for r in t_rows]

    def get_cross_table_binding(self, std_table_key: str) -> Optional[Dict[str, Any]]:
        key_lower = (std_table_key or "").strip().lower()
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM cross_table_bindings WHERE std_table_key = ?",
                (key_lower,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_cross_table_binding(
        self,
        std_table_name: str,
        *,
        std_schema_name: str = "public",
        source_schema: str = "",
        source_table: str = "",
        target_schema: str = "",
        target_table: str = "",
        std_table_comment: str = "",
    ) -> Dict[str, Any]:
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        clean_std_table = str(std_table_name or "").strip(quote_chars)
        if not clean_std_table:
            raise ValueError("标准表名不能为空")
        t_key = clean_std_table.lower()
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM cross_table_bindings WHERE std_table_key = ?",
                (t_key,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE cross_table_bindings
                    SET source_schema = ?,
                        source_table = ?,
                        target_schema = ?,
                        target_table = ?,
                        std_table_comment = CASE WHEN ? != '' THEN ? ELSE std_table_comment END,
                        updated_at = ?
                    WHERE std_table_key = ?
                    """,
                    (
                        str(source_schema).strip(quote_chars),
                        str(source_table).strip(quote_chars),
                        str(target_schema).strip(quote_chars),
                        str(target_table).strip(quote_chars),
                        std_table_comment,
                        std_table_comment,
                        now,
                        t_key,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO cross_table_bindings (
                        std_table_key, std_schema_name, std_table_name, std_table_comment,
                        source_schema, source_table, target_schema, target_table, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        t_key,
                        str(std_schema_name or "public").strip(quote_chars),
                        clean_std_table,
                        std_table_comment,
                        str(source_schema).strip(quote_chars) or str(std_schema_name or "public").strip(quote_chars),
                        str(source_table).strip(quote_chars) or clean_std_table,
                        str(target_schema).strip(quote_chars) or str(std_schema_name or "public").strip(quote_chars),
                        str(target_table).strip(quote_chars) or clean_std_table,
                        now,
                    ),
                )
            conn.commit()
        return self.get_cross_table_binding(t_key) or {}

    def list_cross_columns(self, std_table_key: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock, self._get_conn() as conn:
            if std_table_key:
                rows = conn.execute(
                    """
                    SELECT * FROM cross_column_bindings
                    WHERE std_table_key = ?
                    ORDER BY row_index ASC, id ASC
                    """,
                    (std_table_key.strip().lower(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cross_column_bindings ORDER BY std_table_key ASC, row_index ASC, id ASC"
                ).fetchall()
            return [dict(r) for r in rows]

    def batch_update_cross_column_bindings(
        self, updates: List[Dict[str, Any]]
    ) -> None:
        if not updates:
            return
        now = self._now_str()
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        with self._lock, self._get_conn() as conn:
            for u in updates:
                cid = int(u["id"])
                bound_src = str(u.get("bound_source_col", "")).strip(quote_chars)
                bound_tgt = str(u.get("bound_target_col", "")).strip(quote_chars)
                conn.execute(
                    """
                    UPDATE cross_column_bindings
                    SET bound_source_col = ?,
                        bound_target_col = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (bound_src, bound_tgt, now, cid),
                )
            conn.commit()

    def clear_cross_standards(self) -> None:
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM cross_table_bindings")
            conn.execute("DELETE FROM cross_column_bindings")
            conn.commit()
        self.save_meta("cross_standard_filename", "未导入")
        self.save_meta("cross_standard_imported_at", "")

    def export_cross_bindings_bundle(self) -> Dict[str, Any]:
        """导出完整跨环境绑定关系配置包（供持久备份或跨机器导入复用，不含明文密码）."""
        src_conn = dict(self.get_meta("cross_source_conn", {}) or {})
        tgt_conn = dict(self.get_meta("cross_target_conn", {}) or {})
        src_conn.pop("password", None)
        tgt_conn.pop("password", None)

        return {
            "bundle_version": "2.0",
            "exported_at": self._now_str(),
            "standard_filename": self.get_meta("cross_standard_filename", "未导入"),
            "source_connection": src_conn,
            "target_connection": tgt_conn,
            "table_bindings": self.list_cross_tables(),
            "column_bindings": self.list_cross_columns(),
        }

    def import_cross_bindings_bundle(
        self, bundle: Dict[str, Any], filename: str = "bindings_bundle.json"
    ) -> Dict[str, Any]:
        """从 JSON 绑定包恢复表级与字段级三方绑定关系."""
        now = self._now_str()
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        table_bindings = bundle.get("table_bindings") or []
        column_bindings = bundle.get("column_bindings") or []
        if not table_bindings and not column_bindings:
            raise ValueError("导入的绑定配置包中未包含有效的 table_bindings 或 column_bindings 数据")

        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM cross_table_bindings")
            conn.execute("DELETE FROM cross_column_bindings")

            t_rows = []
            seen_keys = set()
            for t in table_bindings:
                t_name = str(t.get("std_table_name") or t.get("table_name") or "").strip(quote_chars)
                if not t_name:
                    continue
                t_key = str(t.get("std_table_key") or t_name).strip(quote_chars).lower()
                if t_key in seen_keys:
                    continue
                seen_keys.add(t_key)
                s_schema = str(t.get("std_schema_name") or "public").strip(quote_chars)
                t_rows.append(
                    (
                        t_key,
                        s_schema,
                        t_name,
                        str(t.get("std_table_comment") or "").strip(),
                        str(t.get("source_schema") or s_schema).strip(quote_chars),
                        str(t.get("source_table") or t_name).strip(quote_chars),
                        str(t.get("target_schema") or s_schema).strip(quote_chars),
                        str(t.get("target_table") or t_name).strip(quote_chars),
                        now,
                    )
                )

            c_rows = []
            for idx, c in enumerate(column_bindings, start=1):
                t_key = str(c.get("std_table_key") or c.get("table_name") or "").strip(quote_chars).lower()
                if not t_key:
                    continue
                if t_key not in seen_keys:
                    seen_keys.add(t_key)
                    t_rows.append(
                        (t_key, "public", t_key, "", "public", t_key, "public", t_key, now)
                    )
                orig_name = str(c.get("orig_col_name") or c.get("old_col_name") or "").strip(quote_chars)
                std_name = str(c.get("std_col_name") or c.get("new_col_name") or orig_name).strip(quote_chars)
                c_rows.append(
                    (
                        t_key,
                        int(c.get("row_index") or idx),
                        orig_name,
                        str(c.get("orig_col_type") or c.get("old_col_type") or "").strip(),
                        str(c.get("orig_col_comment") or c.get("old_col_comment") or "").strip(),
                        std_name,
                        str(c.get("std_col_type") or c.get("new_col_type") or "").strip(),
                        str(c.get("std_col_comment") or c.get("new_col_comment") or "").strip(),
                        str(c.get("bound_source_col") or orig_name or std_name).strip(quote_chars),
                        str(c.get("bound_target_col") or std_name or orig_name).strip(quote_chars),
                        now,
                    )
                )

            conn.executemany(
                """
                INSERT INTO cross_table_bindings (
                    std_table_key, std_schema_name, std_table_name, std_table_comment,
                    source_schema, source_table, target_schema, target_table, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                t_rows,
            )
            conn.executemany(
                """
                INSERT INTO cross_column_bindings (
                    std_table_key, row_index,
                    orig_col_name, orig_col_type, orig_col_comment,
                    std_col_name, std_col_type, std_col_comment,
                    bound_source_col, bound_target_col, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                c_rows,
            )
            conn.commit()

        std_fn = str(bundle.get("standard_filename") or filename)
        self.save_meta("cross_standard_filename", std_fn)
        self.save_meta("cross_standard_imported_at", now)
        return {
            "table_count": len(t_rows),
            "column_count": len(c_rows),
            "standard_filename": std_fn,
        }
