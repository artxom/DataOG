"""单库落标贯标模块 — 共享工作区变更日志、Checkpoint 还原点与物理回滚混入类."""

from __future__ import annotations

import datetime
import io
import json
import threading
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core.dws_engine import DWSMigratorEngine
from core.platform.dialects import (
    analyze_item_changes,
    classify_migration_risk,
    clean_identifier_input,
    normalize_dws_type,
)
from core.platform.excel_io import (
    sanitize_excel_cell,
    sanitize_excel_row,
    sanitize_excel_sheet_title,
)
from core.platform.import_progress import ProgressCallback, noop_progress

MIGRATION_CHANGE_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS migration_change_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_uid TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL DEFAULT '',
    execution_group_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'MIGRATION',
    status TEXT NOT NULL DEFAULT 'APPLIED',
    connection_id TEXT NOT NULL DEFAULT '',
    connection_name TEXT NOT NULL DEFAULT '',
    db_type TEXT NOT NULL DEFAULT '',
    schema_name TEXT NOT NULL DEFAULT 'public',
    table_name TEXT NOT NULL DEFAULT '',
    source_item_id INTEGER NOT NULL DEFAULT 0,
    ordinal_position INTEGER NOT NULL DEFAULT 0,
    std_code TEXT NOT NULL DEFAULT '',
    before_col_name TEXT NOT NULL DEFAULT '',
    before_col_type TEXT NOT NULL DEFAULT '',
    before_col_comment TEXT NOT NULL DEFAULT '',
    after_col_name TEXT NOT NULL DEFAULT '',
    after_col_type TEXT NOT NULL DEFAULT '',
    after_col_comment TEXT NOT NULL DEFAULT '',
    name_changed INTEGER NOT NULL DEFAULT 0,
    type_changed INTEGER NOT NULL DEFAULT 0,
    comment_changed INTEGER NOT NULL DEFAULT 0,
    risk_level TEXT NOT NULL DEFAULT 'L4',
    risk_rank INTEGER NOT NULL DEFAULT 4,
    risk_code TEXT NOT NULL DEFAULT 'SAFE_CHANGE',
    risk_label TEXT NOT NULL DEFAULT '',
    risk_factors_json TEXT NOT NULL DEFAULT '[]',
    is_lossy INTEGER NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL DEFAULT 'DIRECT_ALTER',
    backup_table_name TEXT NOT NULL DEFAULT '',
    forward_sql TEXT NOT NULL DEFAULT '',
    rollback_sql TEXT NOT NULL DEFAULT '',
    rollback_of_change_id INTEGER NOT NULL DEFAULT 0,
    rolled_back_by_id INTEGER NOT NULL DEFAULT 0,
    rolled_back_at TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mcl_conn_table
    ON migration_change_logs (connection_id, schema_name, table_name, id DESC);
CREATE INDEX IF NOT EXISTS idx_mcl_status
    ON migration_change_logs (event_type, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_mcl_group
    ON migration_change_logs (execution_group_id);
"""

MIGRATION_CHANGE_LOGS_COLUMNS: List[Tuple[str, str]] = [
    ("change_uid", "TEXT NOT NULL DEFAULT ''"),
    ("batch_id", "TEXT NOT NULL DEFAULT ''"),
    ("execution_group_id", "TEXT NOT NULL DEFAULT ''"),
    ("event_type", "TEXT NOT NULL DEFAULT 'MIGRATION'"),
    ("status", "TEXT NOT NULL DEFAULT 'APPLIED'"),
    ("connection_id", "TEXT NOT NULL DEFAULT ''"),
    ("connection_name", "TEXT NOT NULL DEFAULT ''"),
    ("db_type", "TEXT NOT NULL DEFAULT ''"),
    ("schema_name", "TEXT NOT NULL DEFAULT 'public'"),
    ("table_name", "TEXT NOT NULL DEFAULT ''"),
    ("source_item_id", "INTEGER NOT NULL DEFAULT 0"),
    ("ordinal_position", "INTEGER NOT NULL DEFAULT 0"),
    ("std_code", "TEXT NOT NULL DEFAULT ''"),
    ("before_col_name", "TEXT NOT NULL DEFAULT ''"),
    ("before_col_type", "TEXT NOT NULL DEFAULT ''"),
    ("before_col_comment", "TEXT NOT NULL DEFAULT ''"),
    ("after_col_name", "TEXT NOT NULL DEFAULT ''"),
    ("after_col_type", "TEXT NOT NULL DEFAULT ''"),
    ("after_col_comment", "TEXT NOT NULL DEFAULT ''"),
    ("name_changed", "INTEGER NOT NULL DEFAULT 0"),
    ("type_changed", "INTEGER NOT NULL DEFAULT 0"),
    ("comment_changed", "INTEGER NOT NULL DEFAULT 0"),
    ("risk_level", "TEXT NOT NULL DEFAULT 'L4'"),
    ("risk_rank", "INTEGER NOT NULL DEFAULT 4"),
    ("risk_code", "TEXT NOT NULL DEFAULT 'SAFE_CHANGE'"),
    ("risk_label", "TEXT NOT NULL DEFAULT ''"),
    ("risk_factors_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("is_lossy", "INTEGER NOT NULL DEFAULT 0"),
    ("strategy", "TEXT NOT NULL DEFAULT 'DIRECT_ALTER'"),
    ("backup_table_name", "TEXT NOT NULL DEFAULT ''"),
    ("forward_sql", "TEXT NOT NULL DEFAULT ''"),
    ("rollback_sql", "TEXT NOT NULL DEFAULT ''"),
    ("rollback_of_change_id", "INTEGER NOT NULL DEFAULT 0"),
    ("rolled_back_by_id", "INTEGER NOT NULL DEFAULT 0"),
    ("rolled_back_at", "TEXT NOT NULL DEFAULT ''"),
    ("note", "TEXT NOT NULL DEFAULT ''"),
    ("created_at", "TEXT NOT NULL DEFAULT ''"),
]

MIGRATION_CHECKPOINTS_DDL = """
CREATE TABLE IF NOT EXISTS migration_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_uid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    connection_id TEXT NOT NULL DEFAULT '',
    connection_name TEXT NOT NULL DEFAULT '',
    anchor_log_id INTEGER NOT NULL DEFAULT 0,
    auto_created INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mcp_conn
    ON migration_checkpoints (connection_id, id DESC);
"""

MIGRATION_CHECKPOINTS_COLUMNS: List[Tuple[str, str]] = [
    ("checkpoint_uid", "TEXT NOT NULL DEFAULT ''"),
    ("name", "TEXT NOT NULL DEFAULT ''"),
    ("description", "TEXT NOT NULL DEFAULT ''"),
    ("connection_id", "TEXT NOT NULL DEFAULT ''"),
    ("connection_name", "TEXT NOT NULL DEFAULT ''"),
    ("anchor_log_id", "INTEGER NOT NULL DEFAULT 0"),
    ("auto_created", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "TEXT NOT NULL DEFAULT ''"),
]


def ensure_migrator_changelog_tables(store: Any) -> None:
    """幂等注册 Layer 3 共享工作区变更日志与 Checkpoint 表."""
    store.register_table_schema(
        "migration_change_logs",
        MIGRATION_CHANGE_LOGS_DDL,
        MIGRATION_CHANGE_LOGS_COLUMNS,
        scope="workspace",
    )
    store.register_table_schema(
        "migration_checkpoints",
        MIGRATION_CHECKPOINTS_DDL,
        MIGRATION_CHECKPOINTS_COLUMNS,
        scope="workspace",
    )


def _now_ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_risk_factors(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x)]
    txt = str(raw or "").strip()
    if not txt:
        return []
    try:
        parsed = json.loads(txt)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if str(x)]
    except Exception:
        pass
    return []


def _format_log_row(row: Any) -> Dict[str, Any]:
    d = dict(row)
    d["id"] = int(d.get("id") or 0)
    d["source_item_id"] = int(d.get("source_item_id") or 0)
    d["ordinal_position"] = int(d.get("ordinal_position") or 0)
    d["name_changed"] = bool(d.get("name_changed"))
    d["type_changed"] = bool(d.get("type_changed"))
    d["comment_changed"] = bool(d.get("comment_changed"))
    d["risk_rank"] = int(d.get("risk_rank") or 4)
    d["is_lossy"] = bool(d.get("is_lossy"))
    d["rollback_of_change_id"] = int(d.get("rollback_of_change_id") or 0)
    d["rolled_back_by_id"] = int(d.get("rolled_back_by_id") or 0)
    d["risk_factors"] = _parse_risk_factors(d.get("risk_factors_json"))
    return d


class _StandardChangelogMixin:
    """变更日志持久化、Checkpoint 还原点与 LIFO 一键回滚编排."""

    _changelog_write_lock = threading.Lock()

    def _ensure_changelog_tables(self) -> None:
        ensure_migrator_changelog_tables(self.ctx.store)

    def _preload_std_code_map(
        self, conn_ids: Sequence[str]
    ) -> Dict[Tuple[str, str, str, str], str]:
        cids = [str(c or "").strip() for c in conn_ids if str(c or "").strip()]
        if not cids:
            return {}
        result: Dict[Tuple[str, str, str, str], str] = {}
        ph = ",".join("?" for _ in cids)
        with self.ctx.store.module_db(scope="workspace") as conn:
            rows = conn.execute(
                f"""
                SELECT connection_id, schema_name, table_name, column_name, std_code
                FROM field_standard_bindings
                WHERE connection_id IN ({ph}) AND TRIM(COALESCE(std_code, '')) != ''
                """,
                tuple(cids),
            ).fetchall()
        for r in rows:
            cid = str(r["connection_id"] or "").strip()
            sch = (clean_identifier_input(str(r["schema_name"] or "")) or "public").lower()
            tbl = (clean_identifier_input(str(r["table_name"] or "")) or "").lower()
            col = (clean_identifier_input(str(r["column_name"] or "")) or "").lower()
            code = str(r["std_code"] or "").strip()
            if cid and tbl and col and code:
                result[(cid, sch, tbl, col)] = code
        return result

    def _resolve_std_code_for_event(
        self,
        std_code_map: Optional[Dict[Tuple[str, str, str, str], str]],
        conn_id: str,
        schema_name: str,
        table_name: str,
        before_col: str,
        after_col: str,
    ) -> str:
        if not std_code_map or not conn_id:
            return ""
        sch = (clean_identifier_input(schema_name) or "public").lower()
        tbl = (clean_identifier_input(table_name) or "").lower()
        for cname in (before_col, after_col):
            col = (clean_identifier_input(cname) or "").lower()
            if col and (conn_id, sch, tbl, col) in std_code_map:
                return std_code_map[(conn_id, sch, tbl, col)]
        return ""

    def _record_committed_change(
        self,
        event: Dict[str, Any],
        *,
        batch_id: str = "",
        conn_id: str = "",
        conn_name: str = "",
        db_type: str = "DWS",
        std_code_map: Optional[Dict[Tuple[str, str, str, str], str]] = None,
    ) -> Dict[str, Any]:
        self._ensure_changelog_tables()
        sch = str(event.get("schema_name") or "public")
        tbl = str(event.get("table_name") or "")
        before_name = str(event.get("before_col_name") or "")
        before_type = str(event.get("before_col_type") or "")
        before_comment = str(event.get("before_col_comment") or "")
        after_name = str(event.get("after_col_name") or before_name)
        after_type = str(event.get("after_col_type") or before_type)
        after_comment = str(event.get("after_col_comment") or "")

        synthetic_item = {
            "schema_name": sch,
            "table_name": tbl,
            "old_col_name": before_name,
            "old_col_type": before_type,
            "old_col_comment": before_comment,
            "new_col_name": after_name,
            "new_col_type": after_type,
            "new_col_comment": after_comment,
            "strategy": str(event.get("strategy") or "DIRECT_ALTER"),
            "table_orientation": str(event.get("table_orientation") or ""),
            "is_distribute_key": bool(event.get("is_distribute_key")),
        }
        changes = analyze_item_changes(synthetic_item)
        risk = classify_migration_risk(synthetic_item, dialect=db_type or "DWS")
        # 若原注释非空且被清空，或注释不同但 analyze_item_changes 因 new_comment=="" 未标记
        comment_changed = bool(changes["comment_changed"]) or (before_comment != after_comment)

        eff_cid = str(event.get("connection_id") or conn_id or "").strip()
        eff_cname = str(event.get("connection_name") or conn_name or "").strip()
        if not eff_cname and eff_cid:
            prof = self.ctx.connection_hub.get_profile(eff_cid)
            if prof:
                eff_cname = prof.name
                db_type = prof.db_type or db_type
        std_code = str(event.get("std_code") or "").strip() or self._resolve_std_code_for_event(
            std_code_map, eff_cid, sch, tbl, before_name, after_name
        )
        now = _now_ts()
        change_uid = f"chg_{uuid.uuid4().hex[:14]}"
        factors_json = json.dumps(risk.get("risk_factors") or [], ensure_ascii=False)

        with self._changelog_write_lock, self.ctx.store.module_db(scope="workspace") as conn:
            cur = conn.execute(
                """
                INSERT INTO migration_change_logs (
                    change_uid, batch_id, execution_group_id, event_type, status,
                    connection_id, connection_name, db_type, schema_name, table_name,
                    source_item_id, ordinal_position, std_code,
                    before_col_name, before_col_type, before_col_comment,
                    after_col_name, after_col_type, after_col_comment,
                    name_changed, type_changed, comment_changed,
                    risk_level, risk_rank, risk_code, risk_label, risk_factors_json, is_lossy,
                    strategy, backup_table_name, forward_sql, rollback_sql,
                    rollback_of_change_id, rolled_back_by_id, rolled_back_at, note, created_at
                ) VALUES (
                    ?, ?, ?, 'MIGRATION', 'APPLIED',
                    ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    0, 0, '', ?, ?
                )
                """,
                (
                    change_uid,
                    batch_id,
                    str(event.get("execution_group_id") or ""),
                    eff_cid,
                    eff_cname,
                    str(db_type or "DWS"),
                    sch,
                    tbl,
                    int(event.get("source_item_id") or 0),
                    int(event.get("ordinal_position") or 0),
                    std_code,
                    before_name,
                    before_type,
                    before_comment,
                    after_name,
                    after_type,
                    after_comment,
                    1 if changes["name_changed"] else 0,
                    1 if changes["type_changed"] else 0,
                    1 if comment_changed else 0,
                    str(risk["risk_level"]),
                    int(risk["risk_rank"]),
                    str(risk["risk_code"]),
                    str(risk["risk_label"]),
                    factors_json,
                    1 if risk["is_lossy"] else 0,
                    str(event.get("strategy") or "DIRECT_ALTER"),
                    str(event.get("backup_table_name") or ""),
                    str(event.get("forward_sql") or ""),
                    str(event.get("rollback_sql") or ""),
                    str(event.get("note") or ""),
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM migration_change_logs WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return _format_log_row(row) if row else {"change_uid": change_uid}

    def list_changelog_state(
        self,
        *,
        connection_id: str = "",
        risk_level: str = "",
        status: str = "",
        keyword: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> Dict[str, Any]:
        self._ensure_changelog_tables()
        eff_cid = (connection_id or "").strip()
        eff_risk = (risk_level or "").strip().upper()
        eff_status = (status or "").strip().upper()
        kw = (keyword or "").strip().lower()
        eff_limit = max(1, min(1000, int(limit or 200)))
        eff_offset = max(0, int(offset or 0))

        with self.ctx.store.module_db(scope="workspace") as conn:
            all_rows = [
                _format_log_row(r)
                for r in conn.execute(
                    "SELECT * FROM migration_change_logs ORDER BY id DESC"
                ).fetchall()
            ]
            cp_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM migration_checkpoints ORDER BY id DESC"
                ).fetchall()
            ]

        applied_count = 0
        rolled_back_count = 0
        rollback_event_count = 0
        risk_counts = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}
        all_risk_counts = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}

        for r in all_rows:
            if eff_cid and r["connection_id"] != eff_cid:
                continue
            ev = str(r.get("event_type") or "").upper()
            st = str(r.get("status") or "").upper()
            rl = str(r.get("risk_level") or "L4").upper()
            if ev == "ROLLBACK":
                rollback_event_count += 1
                continue
            if rl in all_risk_counts:
                all_risk_counts[rl] += 1
            if st == "APPLIED":
                applied_count += 1
                if rl in risk_counts:
                    risk_counts[rl] += 1
            elif st == "ROLLED_BACK":
                rolled_back_count += 1

        filtered: List[Dict[str, Any]] = []
        for r in all_rows:
            if eff_cid and r["connection_id"] != eff_cid:
                continue
            if eff_risk and eff_risk != "ALL" and r["risk_level"].upper() != eff_risk:
                continue
            if eff_status and eff_status != "ALL":
                if eff_status == "ROLLBACK_EVENT":
                    if r["event_type"].upper() != "ROLLBACK":
                        continue
                else:
                    if r["event_type"].upper() != "MIGRATION" or r["status"].upper() != eff_status:
                        continue
            if kw:
                haystack = " ".join(
                    [
                        r.get("schema_name", ""),
                        r.get("table_name", ""),
                        r.get("before_col_name", ""),
                        r.get("after_col_name", ""),
                        r.get("before_col_type", ""),
                        r.get("after_col_type", ""),
                        r.get("std_code", ""),
                        r.get("backup_table_name", ""),
                        r.get("note", ""),
                    ]
                ).lower()
                if kw not in haystack:
                    continue
            filtered.append(r)

        checkpoints: List[Dict[str, Any]] = []
        for cp in cp_rows:
            cp_cid = str(cp.get("connection_id") or "").strip()
            if eff_cid and cp_cid and cp_cid != eff_cid:
                continue
            anchor_id = int(cp.get("anchor_log_id") or 0)
            since_applied = 0
            since_risks = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}
            for r in all_rows:
                if (
                    r["id"] > anchor_id
                    and r["event_type"] == "MIGRATION"
                    and r["status"] == "APPLIED"
                    and (not cp_cid or r["connection_id"] == cp_cid)
                ):
                    since_applied += 1
                    rl = r["risk_level"]
                    if rl in since_risks:
                        since_risks[rl] += 1
            checkpoints.append(
                {
                    "id": int(cp["id"]),
                    "checkpoint_uid": cp["checkpoint_uid"],
                    "name": cp["name"],
                    "description": cp["description"],
                    "connection_id": cp_cid,
                    "connection_name": cp["connection_name"],
                    "anchor_log_id": anchor_id,
                    "auto_created": bool(cp["auto_created"]),
                    "created_at": cp["created_at"],
                    "changes_since_count": since_applied,
                    "risk_counts_since": since_risks,
                }
            )

        page = filtered[eff_offset : eff_offset + eff_limit]
        return {
            "ok": True,
            "logs": page,
            "total": len(filtered),
            "summary": {
                "total_events": len(all_rows),
                "applied_count": applied_count,
                "rolled_back_count": rolled_back_count,
                "rollback_event_count": rollback_event_count,
                "risk_counts": risk_counts,
                "all_risk_counts": all_risk_counts,
            },
            "checkpoints": checkpoints,
            "connections": self.ctx.connection_hub.list_profiles(),
        }

    def create_checkpoint(
        self,
        name: str,
        *,
        description: str = "",
        connection_id: str = "",
        auto_created: bool = False,
    ) -> Dict[str, Any]:
        self._ensure_changelog_tables()
        clean_name = str(name or "").strip()
        if not clean_name:
            if auto_created:
                clean_name = f"批量执行前自动点 {datetime.datetime.now().strftime('%m-%d %H:%M:%S')}"
            else:
                raise ValueError("请输入 Checkpoint 名称")
        eff_cid = str(connection_id or "").strip()
        eff_cname = ""
        if eff_cid:
            prof = self.ctx.connection_hub.get_profile(eff_cid)
            if prof:
                eff_cname = prof.name
        now = _now_ts()
        cp_uid = f"cp_{uuid.uuid4().hex[:12]}"

        with self._changelog_write_lock, self.ctx.store.module_db(scope="workspace") as conn:
            max_row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM migration_change_logs"
            ).fetchone()
            anchor_id = int(max_row["max_id"] or 0) if max_row else 0
            cur = conn.execute(
                """
                INSERT INTO migration_checkpoints (
                    checkpoint_uid, name, description, connection_id, connection_name,
                    anchor_log_id, auto_created, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cp_uid,
                    clean_name,
                    str(description or "").strip(),
                    eff_cid,
                    eff_cname,
                    anchor_id,
                    1 if auto_created else 0,
                    now,
                ),
            )
            cp = dict(
                conn.execute(
                    "SELECT * FROM migration_checkpoints WHERE id = ?",
                    (cur.lastrowid,),
                ).fetchone()
            )
        cp["id"] = int(cp["id"])
        cp["anchor_log_id"] = int(cp["anchor_log_id"] or 0)
        cp["auto_created"] = bool(cp["auto_created"])
        cp["changes_since_count"] = 0
        cp["risk_counts_since"] = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}
        return {
            "ok": True,
            "checkpoint": cp,
            "message": f"已创建还原点 [{clean_name}]（锚点日志 #{anchor_id}）",
        }

    def delete_checkpoint(self, checkpoint_id: int) -> Dict[str, Any]:
        self._ensure_changelog_tables()
        with self._changelog_write_lock, self.ctx.store.module_db(scope="workspace") as conn:
            row = conn.execute(
                "SELECT id, name FROM migration_checkpoints WHERE id = ?",
                (int(checkpoint_id),),
            ).fetchone()
            if not row:
                raise ValueError(f"未找到 ID={checkpoint_id} 的 Checkpoint")
            conn.execute(
                "DELETE FROM migration_checkpoints WHERE id = ?",
                (int(checkpoint_id),),
            )
        return {
            "ok": True,
            "deleted_id": int(checkpoint_id),
            "message": f"已删除 Checkpoint [{row['name']}]（历史变更日志已保留）",
        }

    def _select_rollback_targets(
        self,
        *,
        checkpoint_id: Optional[int] = None,
        change_ids: Optional[List[int]] = None,
        prefer_backup_table: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_changelog_tables()
        with self.ctx.store.module_db(scope="workspace") as conn:
            all_applied = [
                _format_log_row(r)
                for r in conn.execute(
                    """
                    SELECT * FROM migration_change_logs
                    WHERE event_type = 'MIGRATION' AND status = 'APPLIED'
                    ORDER BY id DESC
                    """
                ).fetchall()
            ]
            checkpoint_info: Optional[Dict[str, Any]] = None
            if checkpoint_id is not None:
                cp_row = conn.execute(
                    "SELECT * FROM migration_checkpoints WHERE id = ?",
                    (int(checkpoint_id),),
                ).fetchone()
                if not cp_row:
                    raise ValueError(f"未找到 ID={checkpoint_id} 的 Checkpoint")
                checkpoint_info = dict(cp_row)
                anchor_id = int(checkpoint_info["anchor_log_id"] or 0)
                cp_cid = str(checkpoint_info.get("connection_id") or "").strip()
                targets = [
                    r
                    for r in all_applied
                    if r["id"] > anchor_id and (not cp_cid or r["connection_id"] == cp_cid)
                ]
                return {
                    "mode": "CHECKPOINT",
                    "checkpoint": checkpoint_info,
                    "targets": targets,
                    "expanded_group_ids": [],
                    "has_conflict": False,
                    "conflict_message": "",
                    "conflicts": [],
                }

            req_ids = [int(x) for x in (change_ids or []) if int(x) > 0]
            if not req_ids:
                raise ValueError("请指定要回滚的 Checkpoint 或变更日志 ID")
            req_set = set(req_ids)
            applied_by_id = {r["id"]: r for r in all_applied}
            missing_or_inactive = [cid for cid in req_ids if cid not in applied_by_id]
            if missing_or_inactive:
                raise ValueError(
                    f"以下变更记录不存在或已回滚，无法再次撤销: {missing_or_inactive}"
                )

            target_set: Set[int] = set(req_set)
            expanded_groups: List[str] = []
            if prefer_backup_table:
                group_ids = {
                    applied_by_id[cid]["execution_group_id"]
                    for cid in req_ids
                    if applied_by_id[cid]["strategy"] == "TABLE_REBUILD"
                    and applied_by_id[cid]["execution_group_id"]
                }
                if group_ids:
                    for r in all_applied:
                        if r["execution_group_id"] in group_ids and r["id"] not in target_set:
                            target_set.add(r["id"])
                            if r["execution_group_id"] not in expanded_groups:
                                expanded_groups.append(r["execution_group_id"])

            targets = [r for r in all_applied if r["id"] in target_set]

            # 冲突检测：同表是否存在更新的、未纳入本次回滚的 APPLIED 变更
            conflicts: List[Dict[str, Any]] = []
            for t in targets:
                t_sch = (clean_identifier_input(t["schema_name"]) or "public").lower()
                t_tbl = (clean_identifier_input(t["table_name"]) or "").lower()
                t_cols = {
                    c
                    for c in (
                        (clean_identifier_input(t["before_col_name"]) or "").lower(),
                        (clean_identifier_input(t["after_col_name"]) or "").lower(),
                    )
                    if c
                }
                is_bak_swap = bool(
                    prefer_backup_table
                    and t["strategy"] == "TABLE_REBUILD"
                    and t["backup_table_name"]
                )
                for newer in all_applied:
                    if newer["id"] <= t["id"] or newer["id"] in target_set:
                        continue
                    if newer["connection_id"] != t["connection_id"]:
                        continue
                    n_sch = (clean_identifier_input(newer["schema_name"]) or "public").lower()
                    n_tbl = (clean_identifier_input(newer["table_name"]) or "").lower()
                    if n_sch != t_sch or n_tbl != t_tbl:
                        continue
                    n_cols = {
                        c
                        for c in (
                            (clean_identifier_input(newer["before_col_name"]) or "").lower(),
                            (clean_identifier_input(newer["after_col_name"]) or "").lower(),
                        )
                        if c
                    }
                    if is_bak_swap or (t_cols & n_cols):
                        conflicts.append(
                            {
                                "target_id": t["id"],
                                "blocking_id": newer["id"],
                                "table": f"{t['schema_name']}.{t['table_name']}",
                                "target_col": f"{t['before_col_name']} → {t['after_col_name']}",
                                "blocking_col": f"{newer['before_col_name']} → {newer['after_col_name']}",
                                "reason": (
                                    f"表 {t['schema_name']}.{t['table_name']} 存在更新的生效变更 #{newer['id']}"
                                    f"（{newer['before_col_name']} → {newer['after_col_name']}），"
                                    + (
                                        "按备份表回滚会覆盖该后续变更"
                                        if is_bak_swap
                                        else "触及同一字段，须先撤销较新的变更或改用 Checkpoint 逆序回滚"
                                    )
                                ),
                            }
                        )
                        break

            conflict_msg = conflicts[0]["reason"] if conflicts else ""
            return {
                "mode": "CHANGES",
                "checkpoint": None,
                "targets": targets,
                "expanded_group_ids": expanded_groups,
                "has_conflict": bool(conflicts),
                "conflict_message": conflict_msg,
                "conflicts": conflicts,
            }

    def preview_rollback(
        self,
        *,
        checkpoint_id: Optional[int] = None,
        change_ids: Optional[List[int]] = None,
        prefer_backup_table: bool = True,
    ) -> Dict[str, Any]:
        sel = self._select_rollback_targets(
            checkpoint_id=checkpoint_id,
            change_ids=change_ids,
            prefer_backup_table=prefer_backup_table,
        )
        targets: List[Dict[str, Any]] = sel["targets"]
        warnings: List[str] = []
        if sel["expanded_group_ids"]:
            warnings.append(
                "所选变更包含整表保序重建批次，使用备份表切回将联动回滚同重建批次的全部列。"
            )

        # 按 LIFO 顺序将同组 TABLE_REBUILD 折叠为表级回滚步骤
        grouped_steps: List[Dict[str, Any]] = []
        seen_groups: Dict[str, Dict[str, Any]] = {}
        for ch in targets:
            gid = ch["execution_group_id"] if ch["strategy"] == "TABLE_REBUILD" else ""
            if gid:
                if gid not in seen_groups:
                    step_obj = {
                        "step_index": len(grouped_steps) + 1,
                        "kind": "REBUILD_GROUP",
                        "execution_group_id": gid,
                        "connection_id": ch["connection_id"],
                        "connection_name": ch["connection_name"],
                        "db_type": ch["db_type"],
                        "schema_name": ch["schema_name"],
                        "table_name": ch["table_name"],
                        "backup_table_name": ch["backup_table_name"],
                        "changes": [ch],
                    }
                    seen_groups[gid] = step_obj
                    grouped_steps.append(step_obj)
                else:
                    seen_groups[gid]["changes"].append(ch)
            else:
                grouped_steps.append(
                    {
                        "step_index": len(grouped_steps) + 1,
                        "kind": "SINGLE_COLUMN",
                        "execution_group_id": "",
                        "connection_id": ch["connection_id"],
                        "connection_name": ch["connection_name"],
                        "db_type": ch["db_type"],
                        "schema_name": ch["schema_name"],
                        "table_name": ch["table_name"],
                        "backup_table_name": "",
                        "changes": [ch],
                    }
                )

        # 缓存已检查过的备份表存在性与连接引擎能力
        bak_exists_cache: Dict[Tuple[str, str, str], str] = {}
        engine_cache: Dict[str, Tuple[DWSMigratorEngine, Any, bool]] = {}
        for step in grouped_steps:
            cid = step["connection_id"]
            if cid not in engine_cache:
                eng, prof = self._get_engine_for_connection(cid)
                can_online = bool(
                    prof
                    and prof.db_type in ("DWS", "PGSQL", "KINGBASE")
                    and prof.host
                    and prof.driver_track == "NATIVE"
                )
                engine_cache[cid] = (eng, prof, can_online)
            eng, prof, can_online = engine_cache[cid]
            step["can_execute_online"] = can_online
            if not step["connection_name"] and prof:
                step["connection_name"] = prof.name

            bak_name = str(step.get("backup_table_name") or "").strip()
            step_warnings: List[str] = []
            if step["kind"] == "REBUILD_GROUP" and prefer_backup_table and bak_name:
                bkey = (cid, step["schema_name"].lower(), bak_name.lower())
                if bkey not in bak_exists_cache:
                    if can_online:
                        try:
                            exists = eng.check_table_exists(step["schema_name"], bak_name)
                            bak_exists_cache[bkey] = "EXISTS" if exists else "MISSING"
                        except Exception:
                            bak_exists_cache[bkey] = "UNKNOWN"
                    else:
                        bak_exists_cache[bkey] = "UNKNOWN"
                bak_status = bak_exists_cache[bkey]
            elif bak_name:
                bak_status = "SKIPPED"
            else:
                bak_status = "NONE"
            step["backup_status"] = bak_status

            if (
                step["kind"] == "REBUILD_GROUP"
                and prefer_backup_table
                and bak_name
                and bak_status in ("EXISTS", "UNKNOWN")
            ):
                step["mode"] = "BACKUP_SWAP"
                step["mode_label"] = "备份表无损切回"
                step["rollback_sql"] = DWSMigratorEngine.build_reverse_backup_swap_sql(
                    step["schema_name"], step["table_name"], bak_name
                )
                if bak_status == "UNKNOWN":
                    step_warnings.append(
                        f"未能在线核验备份表 {bak_name} 是否存在；执行时若缺失将自动降级为逐列逆向 DDL。"
                    )
            else:
                step["mode"] = "REVERSE_ALTER"
                step["mode_label"] = "逆向 ALTER DDL"
                if step["kind"] == "REBUILD_GROUP" and bak_name and bak_status == "MISSING":
                    step_warnings.append(
                        f"备份表 {bak_name} 在目标库中已不存在，已自动降级为逐列逆向 DDL。"
                    )
                elif step["kind"] == "REBUILD_GROUP" and not bak_name:
                    step_warnings.append(
                        "该次整表重建未保留备份表，将按逆序逐列执行原地逆向 DDL。"
                    )
                sql_parts = []
                for ch in step["changes"]:
                    sql_parts.append(
                        DWSMigratorEngine.build_reverse_direct_alter_sql(
                            ch["schema_name"],
                            ch["table_name"],
                            before_col_name=ch["before_col_name"],
                            before_col_type=ch["before_col_type"],
                            before_col_comment=ch["before_col_comment"],
                            after_col_name=ch["after_col_name"],
                            after_col_type=ch["after_col_type"],
                            after_col_comment=ch["after_col_comment"],
                            dialect=ch.get("db_type") or "DWS",
                        )
                    )
                step["rollback_sql"] = "\n".join(sql_parts)
                if any(ch["is_lossy"] for ch in step["changes"]):
                    step_warnings.append(
                        "含 L2 类型转换或 L3 精度/长度缩减：原地逆向 DDL 仅恢复字段类型与结构，无法还原已截断的旧精度数值。"
                    )

            step["warnings"] = step_warnings
            for w in step_warnings:
                if w not in warnings:
                    warnings.append(w)

            step["change_ids"] = [int(ch["id"]) for ch in step["changes"]]
            step["summary_text"] = (
                f"{step['schema_name']}.{step['table_name']} · "
                + (
                    f"备份表 {bak_name} 切回（{len(step['changes'])} 列）"
                    if step["mode"] == "BACKUP_SWAP"
                    else " / ".join(
                        f"{ch['after_col_name']} → {ch['before_col_name']}"
                        for ch in step["changes"]
                    )
                )
            )
            step["changes_preview"] = [
                {
                    "id": ch["id"],
                    "ordinal_position": ch["ordinal_position"],
                    "before_col_name": ch["before_col_name"],
                    "before_col_type": ch["before_col_type"],
                    "before_col_comment": ch["before_col_comment"],
                    "after_col_name": ch["after_col_name"],
                    "after_col_type": ch["after_col_type"],
                    "after_col_comment": ch["after_col_comment"],
                    "risk_level": ch["risk_level"],
                    "risk_label": ch["risk_label"],
                    "is_lossy": ch["is_lossy"],
                }
                for ch in step["changes"]
            ]
            # 保留紧凑输出，不冗余返回整份 changes ORM 字典
            step.pop("changes", None)

        combined_sql = "\n\n".join(
            f"-- Step {s['step_index']}: {s['summary_text']} [{s['mode_label']}]\n{s['rollback_sql']}"
            for s in grouped_steps
        )
        can_all_online = bool(grouped_steps) and all(
            s["can_execute_online"] for s in grouped_steps
        )
        return {
            "ok": True,
            "mode": sel["mode"],
            "checkpoint": sel["checkpoint"],
            "prefer_backup_table": bool(prefer_backup_table),
            "has_conflict": sel["has_conflict"],
            "conflict_message": sel["conflict_message"],
            "conflicts": sel["conflicts"],
            "target_change_count": len(targets),
            "step_count": len(grouped_steps),
            "steps": grouped_steps,
            "warnings": warnings,
            "combined_rollback_sql": combined_sql,
            "can_execute_all_online": can_all_online,
        }

    def _post_rollback_sync(
        self,
        touched_steps: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """回滚提交成功后，按逆序还原字段标准绑定、刷新表元数据及核验队列状态."""
        touched_tables: Dict[Tuple[str, str, str], Dict[str, str]] = {}
        for item in touched_steps:
            cid = str(item["connection_id"] or "").strip()
            sch = clean_identifier_input(str(item["schema_name"] or "")) or "public"
            tbl = clean_identifier_input(str(item["table_name"] or ""))
            if not cid or not tbl:
                continue
            touched_tables[(cid, sch.lower(), tbl.lower())] = {
                "connection_id": cid,
                "schema_name": sch,
                "table_name": tbl,
            }
            renames = item.get("renames") or []
            if renames:
                try:
                    self.ctx.standard_catalog.migrate_renamed_column_bindings(
                        cid, sch, tbl, renames
                    )
                except Exception:
                    pass

        for tbl_info in touched_tables.values():
            try:
                self.get_table_compliance(
                    tbl_info["connection_id"],
                    tbl_info["schema_name"],
                    tbl_info["table_name"],
                    force_refresh=True,
                    include_table_stats=False,
                    purge_errors=False,
                )
            except Exception:
                pass

        try:
            self.sync_items(tolerate_conn_errors=True)
        except Exception:
            pass

        touched_cids = list({v["connection_id"] for v in touched_tables.values() if v["connection_id"]})
        table_stats_by_conn = {
            cid: self.ctx.standard_catalog.get_connection_binding_stats(cid)
            for cid in touched_cids
        }
        return {
            "table_stats_by_conn": table_stats_by_conn,
            "rolled_back_tables": list(touched_tables.values()),
        }

    def _mark_changes_rolled_back(
        self,
        *,
        step: Dict[str, Any],
        executed_sql: str,
        mode_used: str,
        checkpoint_id: Optional[int] = None,
    ) -> int:
        change_ids = [int(x) for x in (step.get("change_ids") or []) if int(x) > 0]
        if not change_ids:
            return 0
        now = _now_ts()
        audit_uid = f"rb_{uuid.uuid4().hex[:14]}"
        first_ch = (step.get("changes_preview") or [{}])[0]
        cp_note = f"Checkpoint #{checkpoint_id} 回滚" if checkpoint_id else "单条/批量变更撤销"
        note = f"{cp_note} ({mode_used})，涉及 {len(change_ids)} 条变更"

        with self._changelog_write_lock, self.ctx.store.module_db(scope="workspace") as conn:
            cur = conn.execute(
                """
                INSERT INTO migration_change_logs (
                    change_uid, batch_id, execution_group_id, event_type, status,
                    connection_id, connection_name, db_type, schema_name, table_name,
                    source_item_id, ordinal_position, std_code,
                    before_col_name, before_col_type, before_col_comment,
                    after_col_name, after_col_type, after_col_comment,
                    name_changed, type_changed, comment_changed,
                    risk_level, risk_rank, risk_code, risk_label, risk_factors_json, is_lossy,
                    strategy, backup_table_name, forward_sql, rollback_sql,
                    rollback_of_change_id, rolled_back_by_id, rolled_back_at, note, created_at
                ) VALUES (
                    ?, '', ?, 'ROLLBACK', 'APPLIED',
                    ?, ?, ?, ?, ?,
                    0, ?, '',
                    ?, ?, ?,
                    ?, ?, ?,
                    0, 0, 0,
                    'L4', 4, 'ROLLBACK', '回滚审计', '[]', 0,
                    ?, ?, '', ?,
                    ?, 0, '', ?, ?
                )
                """,
                (
                    audit_uid,
                    str(step.get("execution_group_id") or ""),
                    str(step.get("connection_id") or ""),
                    str(step.get("connection_name") or ""),
                    str(step.get("db_type") or "DWS"),
                    str(step.get("schema_name") or "public"),
                    str(step.get("table_name") or ""),
                    int(first_ch.get("ordinal_position") or 0),
                    str(first_ch.get("after_col_name") or ""),
                    str(first_ch.get("after_col_type") or ""),
                    str(first_ch.get("after_col_comment") or ""),
                    str(first_ch.get("before_col_name") or ""),
                    str(first_ch.get("before_col_type") or ""),
                    str(first_ch.get("before_col_comment") or ""),
                    mode_used,
                    str(step.get("backup_table_name") or ""),
                    executed_sql,
                    int(change_ids[0]),
                    note,
                    now,
                ),
            )
            audit_id = int(cur.lastrowid or 0)
            ph = ",".join("?" for _ in change_ids)
            conn.execute(
                f"""
                UPDATE migration_change_logs
                SET status = 'ROLLED_BACK',
                    rolled_back_by_id = ?,
                    rolled_back_at = ?
                WHERE id IN ({ph}) AND status = 'APPLIED'
                """,
                (audit_id, now, *change_ids),
            )
            return audit_id

    def execute_rollback(
        self,
        *,
        checkpoint_id: Optional[int] = None,
        change_ids: Optional[List[int]] = None,
        prefer_backup_table: bool = True,
        progress_cb: ProgressCallback = noop_progress,
    ) -> Dict[str, Any]:
        report = progress_cb or noop_progress
        report(12.0, "生成回滚执行计划", "正在核验回滚顺序、冲突与备份表状态...", 0, 1)
        plan = self.preview_rollback(
            checkpoint_id=checkpoint_id,
            change_ids=change_ids,
            prefer_backup_table=prefer_backup_table,
        )
        if plan["has_conflict"]:
            raise ValueError(plan["conflict_message"] or "存在较新的依赖变更冲突，请先处理后再回滚")
        steps: List[Dict[str, Any]] = plan["steps"]
        if not steps:
            raise ValueError("当前范围内没有可回滚的生效变更记录")

        for s in steps:
            if not s["can_execute_online"]:
                cname = s.get("connection_name") or s.get("connection_id") or "未知连接"
                raise ValueError(
                    f"数据源 [{cname}]（{s.get('db_type') or 'DB'}）暂不支持在线直连自动回滚，请复制预览回滚 SQL 手工执行"
                )

        total_steps = len(steps)
        completed_steps = 0
        rolled_back_change_count = 0
        touched_steps: List[Dict[str, Any]] = []
        failed_step_info: Optional[Dict[str, Any]] = None
        err_message = ""

        for idx, step in enumerate(steps, start=1):
            pct = 18.0 + 68.0 * ((idx - 1) / max(1, total_steps))
            report(
                pct,
                "按逆序执行物理回滚",
                f"[{idx}/{total_steps}] 正在回滚 {step['summary_text']}...",
                idx - 1,
                total_steps,
            )
            eng, _prof = self._get_engine_for_connection(step["connection_id"])
            try:
                mode_used = step["mode"]
                bak_name = str(step.get("backup_table_name") or "").strip()
                if mode_used == "BACKUP_SWAP" and bak_name:
                    if not eng.check_table_exists(step["schema_name"], bak_name):
                        mode_used = "REVERSE_ALTER"

                if mode_used == "BACKUP_SWAP":
                    rb_res = eng.execute_rollback_from_backup_table(
                        schema_name=step["schema_name"],
                        table_name=step["table_name"],
                        backup_table_name=bak_name,
                    )
                    exec_sql = str(rb_res.get("executed_sql") or step["rollback_sql"])
                else:
                    exec_sql_parts: List[str] = []
                    for ch in step.get("changes_preview") or []:
                        rb_res = eng.execute_rollback_direct_alter(
                            schema_name=step["schema_name"],
                            table_name=step["table_name"],
                            before_col_name=ch["before_col_name"],
                            before_col_type=ch["before_col_type"],
                            before_col_comment=ch["before_col_comment"],
                            after_col_name=ch["after_col_name"],
                            after_col_type=ch["after_col_type"],
                            after_col_comment=ch["after_col_comment"],
                            expected_ordinal=int(ch.get("ordinal_position") or 0),
                        )
                        exec_sql_parts.append(str(rb_res.get("executed_sql") or ""))
                    exec_sql = "\n".join(p for p in exec_sql_parts if p)

                self._mark_changes_rolled_back(
                    step=step,
                    executed_sql=exec_sql,
                    mode_used=mode_used,
                    checkpoint_id=checkpoint_id,
                )
                self.ctx.store.add_log(
                    "SUCCESS",
                    f"{step['schema_name']}.{step['table_name']} (回滚)",
                    f"已完成回滚步骤 [{step['summary_text']}] ({mode_used})",
                    exec_sql,
                )
                renames = [
                    (ch["after_col_name"], ch["before_col_name"])
                    for ch in (step.get("changes_preview") or [])
                    if clean_identifier_input(ch["after_col_name"]).lower()
                    != clean_identifier_input(ch["before_col_name"]).lower()
                ]
                touched_steps.append(
                    {
                        "connection_id": step["connection_id"],
                        "schema_name": step["schema_name"],
                        "table_name": step["table_name"],
                        "renames": renames,
                    }
                )
                completed_steps += 1
                rolled_back_change_count += len(step.get("change_ids") or [])
            except Exception as exc:
                err_message = str(exc).strip() or "回滚执行失败"
                failed_step_info = {
                    "step_index": idx,
                    "summary_text": step["summary_text"],
                    "schema_name": step["schema_name"],
                    "table_name": step["table_name"],
                    "error": err_message,
                }
                self.ctx.store.add_log(
                    "ERROR",
                    f"{step['schema_name']}.{step['table_name']} (回滚断点)",
                    f"第 {idx}/{total_steps} 步回滚中断（已保留断点，前面 {completed_steps} 步已生效）：{err_message}",
                    step.get("rollback_sql", ""),
                )
                break

        sync_meta: Dict[str, Any] = {"table_stats_by_conn": {}, "rolled_back_tables": []}
        if touched_steps:
            report(
                90.0,
                "还原字段绑标与刷新队列",
                "正在逆向还原已回滚表的字段标准绑定并刷新队列核验状态...",
                completed_steps,
                total_steps,
            )
            sync_meta = self._post_rollback_sync(touched_steps)

        ws_state = self.get_workspace_state()
        self._attach_post_migration_sync_state(ws_state, sync_meta)
        changelog_state = self.list_changelog_state()

        if failed_step_info is not None:
            warn_msg = (
                f"回滚在第 {failed_step_info['step_index']}/{total_steps} 步遇到异常已停止（保留断点）："
                f"{err_message}；此前已完成 {completed_steps} 步（共 {rolled_back_change_count} 条变更已回滚），修复问题后可继续回滚剩余项。"
            )
            return {
                "ok": False,
                "partial_rolled_back": completed_steps > 0,
                "completed_steps": completed_steps,
                "total_steps": total_steps,
                "rolled_back_change_count": rolled_back_change_count,
                "failed_step": failed_step_info,
                "error": err_message,
                "warning": warn_msg,
                "workspace_state": ws_state,
                "changelog_state": changelog_state,
                "table_stats_by_conn": sync_meta.get("table_stats_by_conn", {}),
            }

        ok_msg = f"回滚完成：共逆序执行 {completed_steps} 步，已恢复 {rolled_back_change_count} 条落标变更并同步还原字段绑标与队列状态。"
        report(98.5, "回滚与状态同步完成", ok_msg, total_steps, total_steps)
        return {
            "ok": True,
            "partial_rolled_back": False,
            "completed_steps": completed_steps,
            "total_steps": total_steps,
            "rolled_back_change_count": rolled_back_change_count,
            "message": ok_msg,
            "workspace_state": ws_state,
            "changelog_state": changelog_state,
            "table_stats_by_conn": sync_meta.get("table_stats_by_conn", {}),
        }

    def _compute_effective_column_mappings(
        self, logs_asc: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """按表折叠生效中的变更链（A→B→C 归并为一个净映射 A→C），供下期 SQL 智能改写底座消费."""
        table_chains: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for r in logs_asc:
            if r["event_type"] != "MIGRATION" or r["status"] != "APPLIED":
                continue
            cid = r["connection_id"]
            sch = clean_identifier_input(r["schema_name"]) or "public"
            tbl = clean_identifier_input(r["table_name"])
            tkey = (cid, sch.lower(), tbl.lower())
            chains = table_chains.setdefault(tkey, [])

            b_name = clean_identifier_input(r["before_col_name"]) or r["before_col_name"]
            a_name = clean_identifier_input(r["after_col_name"]) or r["after_col_name"]
            matched = None
            for item in chains:
                if item["current_col_name"].lower() == b_name.lower():
                    matched = item
                    break
            if matched is not None:
                matched["current_col_name"] = a_name
                matched["current_col_type"] = r["after_col_type"] or matched["current_col_type"]
                matched["current_col_comment"] = r["after_col_comment"]
                if r["std_code"]:
                    matched["std_code"] = r["std_code"]
                matched["change_ids"].append(r["id"])
                matched["latest_change_at"] = r["created_at"]
                if r["risk_rank"] < matched["highest_risk_rank"]:
                    matched["highest_risk_rank"] = r["risk_rank"]
                    matched["highest_risk_level"] = r["risk_level"]
            else:
                chains.append(
                    {
                        "connection_id": cid,
                        "connection_name": r["connection_name"],
                        "db_type": r["db_type"],
                        "schema_name": sch,
                        "table_name": tbl,
                        "original_col_name": b_name,
                        "original_col_type": r["before_col_type"],
                        "original_col_comment": r["before_col_comment"],
                        "current_col_name": a_name,
                        "current_col_type": r["after_col_type"],
                        "current_col_comment": r["after_col_comment"],
                        "std_code": r["std_code"],
                        "highest_risk_level": r["risk_level"],
                        "highest_risk_rank": r["risk_rank"],
                        "change_ids": [r["id"]],
                        "latest_change_at": r["created_at"],
                    }
                )

        result: List[Dict[str, Any]] = []
        for chains in table_chains.values():
            for entry in chains:
                entry["name_changed"] = (
                    entry["original_col_name"].lower() != entry["current_col_name"].lower()
                )
                entry["type_changed"] = normalize_dws_type(
                    entry["original_col_type"]
                ) != normalize_dws_type(entry["current_col_type"])
                entry["comment_changed"] = (
                    entry["original_col_comment"] != entry["current_col_comment"]
                )
                result.append(entry)
        return result

    def export_changelog_json_bytes(
        self,
        *,
        connection_id: str = "",
        only_applied: bool = True,
    ) -> bytes:
        """导出供下期 SQL 智能改写引擎及外部系统使用的标准结构化变更台账 JSON."""
        self._ensure_changelog_tables()
        eff_cid = (connection_id or "").strip()
        with self.ctx.store.module_db(scope="workspace") as conn:
            logs_asc = [
                _format_log_row(r)
                for r in conn.execute(
                    "SELECT * FROM migration_change_logs ORDER BY id ASC"
                ).fetchall()
            ]
            checkpoints = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM migration_checkpoints ORDER BY id ASC"
                ).fetchall()
            ]

        if eff_cid:
            logs_asc = [r for r in logs_asc if r["connection_id"] == eff_cid]
            checkpoints = [
                cp
                for cp in checkpoints
                if not str(cp.get("connection_id") or "").strip()
                or cp["connection_id"] == eff_cid
            ]

        effective_mappings = self._compute_effective_column_mappings(logs_asc)
        exported_changes = (
            [
                r
                for r in logs_asc
                if r["event_type"] == "MIGRATION" and r["status"] == "APPLIED"
            ]
            if only_applied
            else logs_asc
        )
        payload = {
            "schema_version": "1.0",
            "exported_at": _now_ts(),
            "connection_filter": eff_cid,
            "only_applied": bool(only_applied),
            "checkpoints": checkpoints,
            "effective_column_mappings": effective_mappings,
            "changes": exported_changes,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def export_changelog_excel_bytes(self, *, connection_id: str = "") -> bytes:
        """导出带双 Sheet（变更日志台账 + Checkpoint 快照）的安全 .xlsx 字节流."""
        self._ensure_changelog_tables()
        eff_cid = (connection_id or "").strip()
        with self.ctx.store.module_db(scope="workspace") as conn:
            logs_desc = [
                _format_log_row(r)
                for r in conn.execute(
                    "SELECT * FROM migration_change_logs ORDER BY id DESC"
                ).fetchall()
            ]
            cps_desc = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM migration_checkpoints ORDER BY id DESC"
                ).fetchall()
            ]

        if eff_cid:
            logs_desc = [r for r in logs_desc if r["connection_id"] == eff_cid]
            cps_desc = [
                cp
                for cp in cps_desc
                if not str(cp.get("connection_id") or "").strip()
                or cp["connection_id"] == eff_cid
            ]

        wb = openpyxl.Workbook()
        ws_logs = wb.active
        ws_logs.title = sanitize_excel_sheet_title("变更日志台账", fallback="变更日志台账")

        headers_logs = [
            "日志ID",
            "发生时间",
            "事件类型",
            "状态",
            "数据源",
            "模式名",
            "表名",
            "原字段名",
            "原物理类型",
            "原注释",
            "新字段名",
            "新物理类型",
            "新注释",
            "标准编码",
            "危险等级",
            "风险说明",
            "执行策略",
            "备份表名",
            "回滚时间",
            "备注",
        ]
        header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
        cell_font = Font(name="Microsoft YaHei", size=10)

        for c_idx, h in enumerate(headers_logs, start=1):
            c = ws_logs.cell(row=1, column=c_idx, value=sanitize_excel_cell(h))
            c.fill = header_fill
            c.font = header_font
            c.alignment = Alignment(horizontal="left", vertical="center")

        for r_idx, r in enumerate(logs_desc, start=2):
            row_vals = sanitize_excel_row(
                [
                    r["id"],
                    r["created_at"],
                    "物理回滚" if r["event_type"] == "ROLLBACK" else "落标改造",
                    "已回滚" if r["status"] == "ROLLED_BACK" else "生效中",
                    r["connection_name"] or r["connection_id"],
                    r["schema_name"],
                    r["table_name"],
                    r["before_col_name"],
                    r["before_col_type"],
                    r["before_col_comment"],
                    r["after_col_name"],
                    r["after_col_type"],
                    r["after_col_comment"],
                    r["std_code"],
                    r["risk_label"] or r["risk_level"],
                    "；".join(r.get("risk_factors") or []),
                    r["strategy"],
                    r["backup_table_name"],
                    r["rolled_back_at"],
                    r["note"],
                ]
            )
            for c_idx, val in enumerate(row_vals, start=1):
                cell = ws_logs.cell(row=r_idx, column=c_idx, value=val)
                cell.font = cell_font

        for c_idx, h in enumerate(headers_logs, start=1):
            ws_logs.column_dimensions[get_column_letter(c_idx)].width = max(
                12, min(36, len(h) * 2 + 6)
            )
        ws_logs.freeze_panes = "A2"

        ws_cp = wb.create_sheet(title=sanitize_excel_sheet_title("Checkpoint快照", fallback="Checkpoint"))
        headers_cp = [
            "Checkpoint ID",
            "名称",
            "类型",
            "数据源",
            "锚点日志ID",
            "创建时间",
            "说明",
        ]
        for c_idx, h in enumerate(headers_cp, start=1):
            c = ws_cp.cell(row=1, column=c_idx, value=sanitize_excel_cell(h))
            c.fill = header_fill
            c.font = header_font
            c.alignment = Alignment(horizontal="left", vertical="center")

        for r_idx, cp in enumerate(cps_desc, start=2):
            row_vals = sanitize_excel_row(
                [
                    cp["id"],
                    cp["name"],
                    "执行前自动点" if cp["auto_created"] else "手动还原点",
                    cp["connection_name"] or cp["connection_id"] or "全部数据源",
                    cp["anchor_log_id"],
                    cp["created_at"],
                    cp["description"],
                ]
            )
            for c_idx, val in enumerate(row_vals, start=1):
                cell = ws_cp.cell(row=r_idx, column=c_idx, value=val)
                cell.font = cell_font
        for c_idx, h in enumerate(headers_cp, start=1):
            ws_cp.column_dimensions[get_column_letter(c_idx)].width = max(
                14, min(38, len(h) * 2 + 8)
            )
        ws_cp.freeze_panes = "A2"

        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()
