"""Data.O.G 数据治理业务持久化仓库：单库贯标队列与跨环境比对映射表 (core/platform/storage/governance_repos.py)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core.platform.dialects import item_requires_table_rebuild


class _GovernanceReposMixin:
    """2.X 数据标准与跨环境比对共享状态仓库."""

    # =========================================================================
    # 单库批量贯标模块 (standard_migrator) 状态接口
    # =========================================================================

    @staticmethod
    def _normalize_migration_item_payload(
        item: Dict[str, Any],
        prev: Optional[Dict[str, Any]] = None,
        *,
        default_connection_id: str = "",
        preserve_prev_errors: bool = True,
    ) -> Dict[str, Any]:
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        schema_name = str(item.get("schema_name", "")).strip(quote_chars) or "public"
        table_name = str(item.get("table_name", "")).strip(quote_chars)
        old_col_name = str(item.get("old_col_name", "")).strip(quote_chars)
        old_col_type = str(item.get("old_col_type", "")).strip()
        old_col_comment = str(item.get("old_col_comment", "")).strip()
        new_col_name = str(item.get("new_col_name", "")).strip(quote_chars) or old_col_name
        new_col_type = str(item.get("new_col_type", "")).strip() or old_col_type
        new_col_comment = str(item.get("new_col_comment", "")).strip()
        conn_id = str(
            item.get("connection_id")
            or (prev.get("connection_id") if prev else "")
            or default_connection_id
            or ""
        ).strip()

        status = item.get("status") or (prev["status"] if prev else "UNCHECKED")
        strategy = str(
            item.get("strategy") or (prev.get("strategy") if prev else "DIRECT_ALTER") or "DIRECT_ALTER"
        )
        status_detail = item.get("status_detail") or (
            prev["status_detail"]
            if prev
            else "已导入清单，等待连接数据库同步元数据状态"
        )
        if preserve_prev_errors:
            error_message = (
                str(item.get("error_message"))
                if item.get("error_message") is not None
                else (prev["error_message"] if prev else "")
            )
            executed_at = (
                str(item.get("executed_at"))
                if item.get("executed_at") is not None
                else (prev["executed_at"] if prev else "")
            )
        else:
            error_message = str(item.get("error_message") or "")
            executed_at = str(item.get("executed_at") or "")

        ordinal_pos = item.get("ordinal_position")
        if ordinal_pos is None and prev:
            ordinal_pos = prev["ordinal_position"]
        total_cols = item.get("total_columns")
        if total_cols is None and prev:
            total_cols = prev["total_columns"]
        db_col = item.get("db_current_col_name") or (
            prev["db_current_col_name"] if prev else ""
        )
        db_type = item.get("db_current_type") or (
            prev["db_current_type"] if prev else ""
        )
        db_comment = (
            item.get("db_current_comment")
            if "db_current_comment" in item and item.get("db_current_comment") is not None
            else (prev["db_current_comment"] if prev else "")
        )
        is_dist = (
            1
            if item.get("is_distribute_key")
            else (int(prev["is_distribute_key"] or 0) if (preserve_prev_errors and prev) else 0)
        )
        orientation = item.get("table_orientation") or (
            prev["table_orientation"] if prev else ""
        )
        generated_sql = str(
            item.get("generated_sql") or (prev.get("generated_sql") if prev else "") or ""
        )
        return {
            "connection_id": conn_id,
            "schema_name": schema_name,
            "table_name": table_name,
            "old_col_name": old_col_name,
            "old_col_type": old_col_type,
            "old_col_comment": old_col_comment,
            "new_col_name": new_col_name,
            "new_col_type": new_col_type,
            "new_col_comment": new_col_comment,
            "status": status,
            "strategy": strategy,
            "ordinal_position": ordinal_pos,
            "total_columns": total_cols,
            "db_current_col_name": db_col,
            "db_current_type": db_type,
            "db_current_comment": db_comment,
            "is_distribute_key": is_dist,
            "table_orientation": orientation,
            "status_detail": status_detail,
            "error_message": error_message,
            "generated_sql": generated_sql,
            "executed_at": executed_at,
        }

    def replace_all_items(
        self,
        items: List[Dict[str, Any]],
        source_filename: str,
        preserve_history: bool = True,
    ) -> List[Dict[str, Any]]:
        now = self._now_str()
        with self._lock, self._get_conn() as conn:
            history_map: Dict[str, Dict[str, Any]] = {}
            if preserve_history:
                existing_rows = conn.execute("SELECT * FROM migration_items").fetchall()
                for r in existing_rows:
                    rd = dict(r)
                    rcid = str(rd.get("connection_id") or "").strip().lower()
                    base_k = f"{rd['schema_name']}.{rd['table_name']}.{rd['old_col_name']}->{rd['new_col_name']}".lower()
                    history_map[f"{rcid}::{base_k}"] = rd
                    history_map.setdefault(base_k, rd)

            conn.execute("DELETE FROM migration_items")
            rows_to_insert = []
            for idx, item in enumerate(items, start=1):
                quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
                s_n = str(item.get("schema_name", "")).strip(quote_chars) or "public"
                t_n = str(item.get("table_name", "")).strip(quote_chars)
                o_n = str(item.get("old_col_name", "")).strip(quote_chars)
                n_n = str(item.get("new_col_name", "")).strip(quote_chars) or o_n
                c_n = str(item.get("connection_id") or "").strip().lower()
                base_k = f"{s_n}.{t_n}.{o_n}->{n_n}".lower()
                prev = history_map.get(f"{c_n}::{base_k}") or history_map.get(base_k)

                norm = self._normalize_migration_item_payload(
                    item, prev=prev, preserve_prev_errors=True
                )
                rows_to_insert.append(
                    (
                        item.get("row_index", idx),
                        norm["connection_id"],
                        norm["schema_name"],
                        norm["table_name"],
                        norm["old_col_name"],
                        norm["old_col_type"],
                        norm["old_col_comment"],
                        norm["new_col_name"],
                        norm["new_col_type"],
                        norm["new_col_comment"],
                        norm["status"],
                        norm["strategy"],
                        norm["ordinal_position"],
                        norm["total_columns"],
                        norm["db_current_col_name"],
                        norm["db_current_type"],
                        norm["db_current_comment"],
                        norm["is_distribute_key"],
                        norm["table_orientation"],
                        norm["status_detail"],
                        norm["error_message"],
                        norm["generated_sql"],
                        norm["executed_at"],
                        now,
                    )
                )

            conn.executemany(
                """
                INSERT INTO migration_items (
                    row_index, connection_id, schema_name, table_name,
                    old_col_name, old_col_type, old_col_comment,
                    new_col_name, new_col_type, new_col_comment,
                    status, strategy, ordinal_position, total_columns,
                    db_current_col_name, db_current_type, db_current_comment,
                    is_distribute_key, table_orientation,
                    status_detail, error_message, generated_sql, executed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def upsert_migration_items(
        self,
        items: List[Dict[str, Any]],
        source_label: str,
        connection_id: str = "",
    ) -> List[Dict[str, Any]]:
        """将字段贯标任务幂等追加或原位更新至全局队列，不删除已有其他表/字段任务."""
        if not items:
            return self.list_items()
        now = self._now_str()
        def_cid = str(connection_id or "").strip()
        active_cid = str(self.get_meta("migrator_connection_id", "") or "").strip()
        with self._lock, self._get_conn() as conn:
            existing_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM migration_items ORDER BY row_index ASC, id ASC"
                ).fetchall()
            ]
            max_row_idx = max((int(r.get("row_index") or 0) for r in existing_rows), default=0)
            existing_by_key: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
            for r in existing_rows:
                r_cid = str(r.get("connection_id") or active_cid or "").strip().lower()
                k = (
                    r_cid,
                    str(r.get("schema_name") or "").strip().lower(),
                    str(r.get("table_name") or "").strip().lower(),
                    str(r.get("old_col_name") or "").strip().lower(),
                )
                existing_by_key[k] = r
                if not str(r.get("connection_id") or "").strip() and def_cid:
                    # 兼容历史未标记 connection_id 的队列项
                    existing_by_key.setdefault(
                        (
                            def_cid.lower(),
                            k[1],
                            k[2],
                            k[3],
                        ),
                        r,
                    )

            to_insert: List[Tuple[Any, ...]] = []
            to_update: List[Tuple[Any, ...]] = []

            for item in items:
                norm = self._normalize_migration_item_payload(
                    item,
                    prev=None,
                    default_connection_id=def_cid or active_cid,
                    preserve_prev_errors=False,
                )
                eff_cid = str(norm["connection_id"] or def_cid or active_cid).strip().lower()
                k = (
                    eff_cid,
                    norm["schema_name"].lower(),
                    norm["table_name"].lower(),
                    norm["old_col_name"].lower(),
                )
                hit = existing_by_key.get(k)
                if hit is not None:
                    to_update.append(
                        (
                            norm["connection_id"],
                            norm["old_col_type"],
                            norm["old_col_comment"],
                            norm["new_col_name"],
                            norm["new_col_type"],
                            norm["new_col_comment"],
                            norm["status"],
                            norm["strategy"],
                            norm["ordinal_position"],
                            norm["total_columns"],
                            norm["db_current_col_name"],
                            norm["db_current_type"],
                            norm["db_current_comment"],
                            norm["is_distribute_key"],
                            norm["table_orientation"],
                            norm["status_detail"],
                            norm["error_message"],
                            norm["generated_sql"],
                            now,
                            int(hit["id"]),
                        )
                    )
                    existing_by_key[k] = {**hit, **norm, "id": hit["id"]}
                else:
                    max_row_idx += 1
                    to_insert.append(
                        (
                            max_row_idx,
                            norm["connection_id"],
                            norm["schema_name"],
                            norm["table_name"],
                            norm["old_col_name"],
                            norm["old_col_type"],
                            norm["old_col_comment"],
                            norm["new_col_name"],
                            norm["new_col_type"],
                            norm["new_col_comment"],
                            norm["status"],
                            norm["strategy"],
                            norm["ordinal_position"],
                            norm["total_columns"],
                            norm["db_current_col_name"],
                            norm["db_current_type"],
                            norm["db_current_comment"],
                            norm["is_distribute_key"],
                            norm["table_orientation"],
                            norm["status_detail"],
                            norm["error_message"],
                            norm["generated_sql"],
                            norm["executed_at"],
                            now,
                        )
                    )
                    existing_by_key[k] = {**norm, "row_index": max_row_idx}

            if to_update:
                conn.executemany(
                    """
                    UPDATE migration_items
                    SET connection_id = ?,
                        old_col_type = ?,
                        old_col_comment = ?,
                        new_col_name = ?,
                        new_col_type = ?,
                        new_col_comment = ?,
                        status = ?,
                        strategy = ?,
                        ordinal_position = ?,
                        total_columns = ?,
                        db_current_col_name = ?,
                        db_current_type = ?,
                        db_current_comment = ?,
                        is_distribute_key = ?,
                        table_orientation = ?,
                        status_detail = ?,
                        error_message = ?,
                        generated_sql = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    to_update,
                )
            if to_insert:
                conn.executemany(
                    """
                    INSERT INTO migration_items (
                        row_index, connection_id, schema_name, table_name,
                        old_col_name, old_col_type, old_col_comment,
                        new_col_name, new_col_type, new_col_comment,
                        status, strategy, ordinal_position, total_columns,
                        db_current_col_name, db_current_type, db_current_comment,
                        is_distribute_key, table_orientation,
                        status_detail, error_message, generated_sql, executed_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    to_insert,
                )
            conn.commit()
        self.save_meta("current_excel_name", source_label)
        self.save_meta("imported_at", now)
        self.add_log(
            "INFO",
            "队列更新",
            f"已将 [{source_label}] 共 {len(items)} 条字段规则纳入全局贯标队列（新增 {len(to_insert)} / 更新 {len(to_update)}）。",
        )
        return self.list_items()

    def delete_migration_items(self, item_ids: List[int]) -> int:
        """按 ID 从全局贯标队列中移除指定任务（不影响已绑定的数据标准）."""
        clean_ids: List[int] = []
        seen = set()
        for raw_id in item_ids or []:
            try:
                iid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if iid > 0 and iid not in seen:
                seen.add(iid)
                clean_ids.append(iid)
        if not clean_ids:
            return 0
        deleted = 0
        with self._lock, self._get_conn() as conn:
            for idx in range(0, len(clean_ids), 500):
                chunk = clean_ids[idx : idx + 500]
                ph = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"DELETE FROM migration_items WHERE id IN ({ph})",
                    chunk,
                )
                deleted += max(0, int(cur.rowcount or 0))
            conn.commit()
        return deleted

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
                strategy = str(u.get("strategy") or "").strip()
                clear_error = bool(u.get("clear_error", False))
                schema_name = u.get("schema_name")
                table_name = u.get("schema_table_override") or u.get("table_name")

                if schema_name and table_name:
                    conn.execute(
                        """
                        UPDATE migration_items
                        SET schema_name = ?,
                            table_name = ?,
                            status = ?,
                            strategy = CASE WHEN ? != '' THEN ? ELSE strategy END,
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
                            strategy,
                            strategy,
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
                            strategy = CASE WHEN ? != '' THEN ? ELSE strategy END,
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
                            strategy,
                            strategy,
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

    def clear_items(self) -> None:
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM migration_items")
            conn.commit()
        self.save_meta("current_excel_name", "未导入")
        self.save_meta("imported_at", "")

    def get_summary(
        self, items: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, int]:
        if items is None:
            items = self.list_items()
        summary = {
            "total": len(items),
            "completed": 0,
            "pending": 0,
            "partial": 0,
            "failed": 0,
            "missing": 0,
            "unchecked": 0,
            "rebuild_required": 0,
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
            if st != "COMPLETED" and item_requires_table_rebuild(item):
                summary["rebuild_required"] += 1
        return summary

    # =========================================================================
    # 跨环境三栏数标比对模块 (cross_comparator) 状态接口
    # =========================================================================

    def replace_cross_standards(
        self,
        items: List[Dict[str, Any]],
        source_filename: str,
        preserve_bindings: bool = True,
    ) -> List[Dict[str, Any]]:
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
                src_std_code = str(
                    item.get("source_std_code")
                    or (prev_c.get("source_std_code", "") if prev_c else "")
                    or ""
                ).strip()
                tgt_std_code = str(
                    item.get("target_std_code")
                    or (prev_c.get("target_std_code", "") if prev_c else "")
                    or ""
                ).strip()

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
                        src_std_code,
                        tgt_std_code,
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
                    bound_source_col, bound_target_col, source_std_code, target_std_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    COUNT(c.id) AS column_count,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN TRIM(COALESCE(c.source_std_code, '')) != ''
                                  OR TRIM(COALESCE(c.target_std_code, '')) != ''
                                THEN 1 ELSE 0
                            END
                        ),
                        0
                    ) AS bound_std_count,
                    COALESCE(GROUP_CONCAT(DISTINCT NULLIF(c.bound_source_col, '')), '') AS source_cols_text,
                    COALESCE(GROUP_CONCAT(DISTINCT NULLIF(c.bound_target_col, '')), '') AS target_cols_text,
                    COALESCE(
                        GROUP_CONCAT(
                            DISTINCT CASE
                                WHEN SUBSTR(COALESCE(c.std_col_name, ''), 1, 11) = '__unpaired_' THEN NULL
                                ELSE NULLIF(c.std_col_name, '')
                            END
                        ),
                        ''
                    ) AS std_cols_text,
                    COALESCE(GROUP_CONCAT(DISTINCT NULLIF(c.std_col_comment, '')), '') AS std_comments_text
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

    def count_cross_columns(self, std_table_key: Optional[str] = None) -> int:
        with self._lock, self._get_conn() as conn:
            if std_table_key:
                row = conn.execute(
                    "SELECT COUNT(*) FROM cross_column_bindings WHERE std_table_key = ?",
                    (std_table_key.strip().lower(),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM cross_column_bindings"
                ).fetchone()
            return int(row[0]) if row else 0

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

    def upsert_cross_table_column_bindings(
        self, std_table_key: str, column_bindings: List[Dict[str, Any]]
    ) -> None:
        """按 id 或 (std_table_key, std_col_name) 幂等更新或插入三方字段桥接关系（兼容导入数标与在线直连表模式）."""
        t_key = (std_table_key or "").strip().lower()
        if not t_key or not column_bindings:
            return
        now = self._now_str()
        quote_chars = '"\'“”‘’`「」『』 \t\r\n\u200b\u3000\ufeff'
        with self._lock, self._get_conn() as conn:
            existing_rows = conn.execute(
                "SELECT id, std_col_name FROM cross_column_bindings WHERE std_table_key = ?",
                (t_key,),
            ).fetchall()
            by_std_col = {
                str(r["std_col_name"] or "").strip(quote_chars).lower(): int(r["id"])
                for r in existing_rows
            }
            next_row_idx = len(existing_rows) + 1
            for item in column_bindings:
                raw_id = int(item.get("id", 0) or 0)
                std_col = str(item.get("std_col_name") or "").strip(quote_chars)
                bound_src = str(item.get("bound_source_col") or "").strip(quote_chars)
                bound_tgt = str(item.get("bound_target_col") or "").strip(quote_chars)
                has_src_std = "source_std_code" in item
                has_tgt_std = "target_std_code" in item
                src_std_code = str(item.get("source_std_code") or "").strip()
                tgt_std_code = str(item.get("target_std_code") or "").strip()
                target_id = raw_id if raw_id > 0 else by_std_col.get(std_col.lower(), 0)
                if target_id > 0:
                    conn.execute(
                        """
                        UPDATE cross_column_bindings
                        SET bound_source_col = ?,
                            bound_target_col = ?,
                            source_std_code = CASE WHEN ? THEN ? ELSE source_std_code END,
                            target_std_code = CASE WHEN ? THEN ? ELSE target_std_code END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            bound_src,
                            bound_tgt,
                            1 if has_src_std else 0,
                            src_std_code,
                            1 if has_tgt_std else 0,
                            tgt_std_code,
                            now,
                            target_id,
                        ),
                    )
                elif std_col:
                    orig_col = str(item.get("orig_col_name") or std_col).strip(quote_chars)
                    std_type = str(item.get("std_col_type") or item.get("orig_col_type") or "").strip()
                    std_comm = str(item.get("std_col_comment") or item.get("orig_col_comment") or "").strip()
                    cur = conn.execute(
                        """
                        INSERT INTO cross_column_bindings (
                            std_table_key, row_index,
                            orig_col_name, orig_col_type, orig_col_comment,
                            std_col_name, std_col_type, std_col_comment,
                            bound_source_col, bound_target_col,
                            source_std_code, target_std_code, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            t_key,
                            next_row_idx,
                            orig_col,
                            std_type,
                            std_comm,
                            std_col,
                            std_type,
                            std_comm,
                            bound_src,
                            bound_tgt,
                            src_std_code,
                            tgt_std_code,
                            now,
                        ),
                    )
                    by_std_col[std_col.lower()] = int(cur.lastrowid or 0)
                    next_row_idx += 1
            conn.commit()

    def clear_cross_standards(self) -> None:
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM cross_table_bindings")
            conn.execute("DELETE FROM cross_column_bindings")
            conn.commit()
        self.save_meta("cross_standard_filename", "未导入")
        self.save_meta("cross_standard_imported_at", "")

    def export_cross_bindings_bundle(self) -> Dict[str, Any]:
        src_conn = dict(self.get_meta("cross_source_conn", {}) or {})
        tgt_conn = dict(self.get_meta("cross_target_conn", {}) or {})
        src_conn.pop("password", None)
        tgt_conn.pop("password", None)

        return {
            "bundle_version": "2.0",
            "exported_at": self._now_str(),
            "standard_filename": self.get_meta("cross_standard_filename", "未导入"),
            "source_connection_id": self.get_meta("cross_source_connection_id", ""),
            "target_connection_id": self.get_meta("cross_target_connection_id", ""),
            "source_connection": src_conn,
            "target_connection": tgt_conn,
            "table_bindings": self.list_cross_tables(),
            "column_bindings": self.list_cross_columns(),
        }

    def import_cross_bindings_bundle(
        self, bundle: Dict[str, Any], filename: str = "bindings_bundle.json"
    ) -> Dict[str, Any]:
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
                        str(c.get("source_std_code") or "").strip(),
                        str(c.get("target_std_code") or "").strip(),
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
                    bound_source_col, bound_target_col,
                    source_std_code, target_std_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

