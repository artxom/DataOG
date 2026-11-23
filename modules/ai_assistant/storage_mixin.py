"""Data.O.G 智能数据助手 Layer 2 私有会话、消息与写操作提案持久化 Mixin."""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional
import uuid


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_json_loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        val = json.loads(raw)
        return val if type(val) is type(default) or default is None else default
    except Exception:
        return default


def _normalize_focus_tables(raw_list: Optional[List[str]]) -> List[str]:
    seen: set[str] = set()
    cleaned: List[str] = []
    for item in raw_list or []:
        s = str(item or "").strip()
        if not s or len(s) > 160:
            continue
        k = s.lower()
        if k not in seen:
            seen.add(k)
            cleaned.append(s)
        if len(cleaned) >= 24:
            break
    return cleaned


class AssistantStorageMixin:
    """管理 `ai_assistant_sessions` / `ai_assistant_messages` / `ai_assistant_proposals` 三张 Layer 2 私有表."""

    @staticmethod
    def ensure_schema(store: Any) -> None:
        store.register_table_schema(
            "ai_assistant_sessions",
            """
            CREATE TABLE IF NOT EXISTS ai_assistant_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                work_mode TEXT NOT NULL DEFAULT 'connection',
                connection_id TEXT NOT NULL DEFAULT '',
                dialect TEXT NOT NULL DEFAULT 'DWS',
                schema_name TEXT NOT NULL DEFAULT '',
                focus_tables_json TEXT NOT NULL DEFAULT '[]',
                task_mode TEXT NOT NULL DEFAULT 'auto',
                provider_id TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                rolling_summary TEXT NOT NULL DEFAULT '',
                turn_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ai_asst_sessions_upd
                ON ai_assistant_sessions(updated_at DESC);
            """,
            [
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("work_mode", "TEXT NOT NULL DEFAULT 'connection'"),
                ("connection_id", "TEXT NOT NULL DEFAULT ''"),
                ("dialect", "TEXT NOT NULL DEFAULT 'DWS'"),
                ("schema_name", "TEXT NOT NULL DEFAULT ''"),
                ("focus_tables_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("task_mode", "TEXT NOT NULL DEFAULT 'auto'"),
                ("provider_id", "TEXT NOT NULL DEFAULT ''"),
                ("model", "TEXT NOT NULL DEFAULT ''"),
                ("rolling_summary", "TEXT NOT NULL DEFAULT ''"),
                ("turn_count", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="private",
        )
        store.register_table_schema(
            "ai_assistant_messages",
            """
            CREATE TABLE IF NOT EXISTS ai_assistant_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL DEFAULT '',
                turn_index INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'user',
                task_mode TEXT NOT NULL DEFAULT 'auto',
                response_type TEXT NOT NULL DEFAULT 'answer',
                content TEXT NOT NULL DEFAULT '',
                steps_json TEXT NOT NULL DEFAULT '[]',
                citations_json TEXT NOT NULL DEFAULT '[]',
                clarification_json TEXT NOT NULL DEFAULT '{}',
                next_steps_json TEXT NOT NULL DEFAULT '[]',
                result_table_json TEXT NOT NULL DEFAULT '{}',
                proposal_id TEXT NOT NULL DEFAULT '',
                provider_name TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                total_elapsed_ms REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ai_asst_msgs_sid
                ON ai_assistant_messages(session_id, id ASC);
            """,
            [
                ("turn_index", "INTEGER NOT NULL DEFAULT 0"),
                ("role", "TEXT NOT NULL DEFAULT 'user'"),
                ("task_mode", "TEXT NOT NULL DEFAULT 'auto'"),
                ("response_type", "TEXT NOT NULL DEFAULT 'answer'"),
                ("content", "TEXT NOT NULL DEFAULT ''"),
                ("steps_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("citations_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("clarification_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("next_steps_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("result_table_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("proposal_id", "TEXT NOT NULL DEFAULT ''"),
                ("provider_name", "TEXT NOT NULL DEFAULT ''"),
                ("model", "TEXT NOT NULL DEFAULT ''"),
                ("total_elapsed_ms", "REAL NOT NULL DEFAULT 0"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="private",
        )
        store.register_table_schema(
            "ai_assistant_proposals",
            """
            CREATE TABLE IF NOT EXISTS ai_assistant_proposals (
                proposal_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                message_id INTEGER NOT NULL DEFAULT 0,
                connection_id TEXT NOT NULL DEFAULT '',
                dialect TEXT NOT NULL DEFAULT '',
                schema_name TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                rationale TEXT NOT NULL DEFAULT '',
                original_sql TEXT NOT NULL DEFAULT '',
                final_sql TEXT NOT NULL DEFAULT '',
                sql_hash TEXT NOT NULL DEFAULT '',
                statements_json TEXT NOT NULL DEFAULT '[]',
                risk_level TEXT NOT NULL DEFAULT 'STANDARD_WRITE',
                category TEXT NOT NULL DEFAULT 'DDL',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                estimated_rows INTEGER DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                execution_result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT '',
                decided_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ai_asst_prop_sid
                ON ai_assistant_proposals(session_id, created_at DESC);
            """,
            [
                ("session_id", "TEXT NOT NULL DEFAULT ''"),
                ("message_id", "INTEGER NOT NULL DEFAULT 0"),
                ("connection_id", "TEXT NOT NULL DEFAULT ''"),
                ("dialect", "TEXT NOT NULL DEFAULT ''"),
                ("schema_name", "TEXT NOT NULL DEFAULT ''"),
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("rationale", "TEXT NOT NULL DEFAULT ''"),
                ("original_sql", "TEXT NOT NULL DEFAULT ''"),
                ("final_sql", "TEXT NOT NULL DEFAULT ''"),
                ("sql_hash", "TEXT NOT NULL DEFAULT ''"),
                ("statements_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("risk_level", "TEXT NOT NULL DEFAULT 'STANDARD_WRITE'"),
                ("category", "TEXT NOT NULL DEFAULT 'DDL'"),
                ("warnings_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("estimated_rows", "INTEGER DEFAULT NULL"),
                ("status", "TEXT NOT NULL DEFAULT 'pending'"),
                ("execution_result_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("decided_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="private",
        )

    @staticmethod
    def _row_to_session_dict(row: Any) -> Dict[str, Any]:
        focus_tables = _safe_json_loads(row["focus_tables_json"], [])
        return {
            "session_id": str(row["session_id"]),
            "title": str(row["title"] or "新对话"),
            "work_mode": str(row["work_mode"] or "connection"),
            "connection_id": str(row["connection_id"] or ""),
            "dialect": str(row["dialect"] or "DWS"),
            "schema_name": str(row["schema_name"] or ""),
            "focus_tables": [str(x) for x in focus_tables if x],
            "task_mode": str(row["task_mode"] or "auto"),
            "provider_id": str(row["provider_id"] or ""),
            "model": str(row["model"] or ""),
            "rolling_summary": str(row["rolling_summary"] or ""),
            "turn_count": int(row["turn_count"] or 0),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    @staticmethod
    def _row_to_proposal_dict(row: Any) -> Dict[str, Any]:
        return {
            "proposal_id": str(row["proposal_id"]),
            "session_id": str(row["session_id"]),
            "message_id": int(row["message_id"] or 0),
            "connection_id": str(row["connection_id"] or ""),
            "dialect": str(row["dialect"] or ""),
            "schema_name": str(row["schema_name"] or ""),
            "title": str(row["title"] or "待确认变更语句"),
            "rationale": str(row["rationale"] or ""),
            "original_sql": str(row["original_sql"] or ""),
            "final_sql": str(row["final_sql"] or row["original_sql"] or ""),
            "sql_hash": str(row["sql_hash"] or ""),
            "statements": _safe_json_loads(row["statements_json"], []),
            "risk_level": str(row["risk_level"] or "STANDARD_WRITE"),
            "category": str(row["category"] or "DDL"),
            "warnings": _safe_json_loads(row["warnings_json"], []),
            "estimated_rows": row["estimated_rows"],
            "status": str(row["status"] or "pending"),
            "execution_result": _safe_json_loads(row["execution_result_json"], {}),
            "created_at": str(row["created_at"] or ""),
            "decided_at": str(row["decided_at"] or ""),
        }

    @staticmethod
    def _row_to_message_dict(row: Any, proposals_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        pid = str(row["proposal_id"] or "")
        return {
            "id": int(row["id"]),
            "session_id": str(row["session_id"]),
            "turn_index": int(row["turn_index"] or 0),
            "role": str(row["role"] or "user"),
            "task_mode": str(row["task_mode"] or "auto"),
            "response_type": str(row["response_type"] or "answer"),
            "content": str(row["content"] or ""),
            "steps": _safe_json_loads(row["steps_json"], []),
            "citations": _safe_json_loads(row["citations_json"], []),
            "clarification": _safe_json_loads(row["clarification_json"], {}),
            "next_steps": _safe_json_loads(row["next_steps_json"], []),
            "result_table": _safe_json_loads(row["result_table_json"], {}),
            "proposal_id": pid,
            "proposal": proposals_by_id.get(pid) if pid else None,
            "provider_name": str(row["provider_name"] or ""),
            "model": str(row["model"] or ""),
            "total_elapsed_ms": float(row["total_elapsed_ms"] or 0.0),
            "created_at": str(row["created_at"] or ""),
        }

    def list_sessions(
        self,
        *,
        connection_id: Optional[str] = None,
        dialect: Optional[str] = None,
        limit: int = 80,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if connection_id:
            clauses.append("connection_id = ?")
            params.append(connection_id.strip())
        if dialect:
            clauses.append("UPPER(dialect) = ?")
            params.append(dialect.strip().upper())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(200, int(limit or 80))))
        with self.ctx.store.module_db(scope="private") as conn:
            rows = conn.execute(
                f"SELECT * FROM ai_assistant_sessions {where_sql} ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row_to_session_dict(r) for r in rows]

    def create_session(
        self,
        *,
        title: str = "",
        work_mode: str = "auto",
        connection_id: str = "",
        dialect: str = "DWS",
        schema_name: str = "",
        focus_tables: Optional[List[str]] = None,
        task_mode: str = "auto",
        provider_id: str = "",
        model: str = "",
    ) -> Dict[str, Any]:
        eff_cid = (connection_id or "").strip()
        eff_dialect = (dialect or "").strip().upper() or "DWS"
        eff_schema = (schema_name or "").strip()
        if eff_cid:
            prof = self.ctx.connection_hub.get_profile(eff_cid)
            if prof is not None:
                eff_dialect = prof.db_type
                if not eff_schema:
                    eff_schema = prof.effective_default_schema()
                eff_mode = "connection"
            else:
                eff_cid = ""
                eff_mode = "dialect"
        else:
            eff_mode = "dialect" if work_mode != "connection" else "dialect"

        sid = f"asst_{uuid.uuid4().hex[:12]}"
        now = _now_str()
        clean_title = (title or "").strip()[:80] or (
            f"{eff_dialect} 智能助手" if eff_mode == "dialect" else "数据库智能会话"
        )
        clean_focus = _normalize_focus_tables(focus_tables)

        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                """
                INSERT INTO ai_assistant_sessions (
                    session_id, title, work_mode, connection_id, dialect, schema_name,
                    focus_tables_json, task_mode, provider_id, model, rolling_summary,
                    turn_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, ?)
                """,
                (
                    sid,
                    clean_title,
                    eff_mode,
                    eff_cid,
                    eff_dialect,
                    eff_schema,
                    json.dumps(clean_focus, ensure_ascii=False),
                    (task_mode or "auto").strip().lower(),
                    (provider_id or "").strip(),
                    (model or "").strip(),
                    now,
                    now,
                ),
            )
        return self.get_session_detail(sid) or {}

    def get_session_detail(self, session_id: str) -> Optional[Dict[str, Any]]:
        sid = (session_id or "").strip()
        if not sid:
            return None
        with self.ctx.store.module_db(scope="private") as conn:
            srow = conn.execute(
                "SELECT * FROM ai_assistant_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
            if srow is None:
                return None
            prows = conn.execute(
                "SELECT * FROM ai_assistant_proposals WHERE session_id = ?", (sid,)
            ).fetchall()
            proposals_by_id = {
                str(pr["proposal_id"]): self._row_to_proposal_dict(pr) for pr in prows
            }
            mrows = conn.execute(
                "SELECT * FROM ai_assistant_messages WHERE session_id = ? ORDER BY id ASC",
                (sid,),
            ).fetchall()
        session_dict = self._row_to_session_dict(srow)
        session_dict["messages"] = [
            self._row_to_message_dict(mr, proposals_by_id) for mr in mrows
        ]
        return session_dict

    def update_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        work_mode: Optional[str] = None,
        connection_id: Optional[str] = None,
        dialect: Optional[str] = None,
        schema_name: Optional[str] = None,
        focus_tables: Optional[List[str]] = None,
        task_mode: Optional[str] = None,
        provider_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        current = self.get_session_detail(session_id)
        if current is None:
            return None
        new_cid = current["connection_id"] if connection_id is None else connection_id.strip()
        new_dialect = current["dialect"] if dialect is None else (dialect.strip().upper() or current["dialect"])
        new_schema = current["schema_name"] if schema_name is None else schema_name.strip()
        new_mode = current["work_mode"]

        if work_mode == "dialect" or (connection_id is not None and not new_cid):
            new_cid = ""
            new_mode = "dialect"
        elif new_cid:
            prof = self.ctx.connection_hub.get_profile(new_cid)
            if prof is not None:
                new_mode = "connection"
                new_dialect = prof.db_type
                if schema_name is None and not new_schema:
                    new_schema = prof.effective_default_schema()
            else:
                new_cid = ""
                new_mode = "dialect"

        new_title = current["title"] if title is None or not title.strip() else title.strip()[:80]
        new_focus = (
            current["focus_tables"]
            if focus_tables is None
            else _normalize_focus_tables(focus_tables)
        )
        new_task_mode = current["task_mode"] if task_mode is None else (task_mode.strip().lower() or "auto")
        new_pid = current["provider_id"] if provider_id is None else provider_id.strip()
        new_mdl = current["model"] if model is None else model.strip()
        now = _now_str()

        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                """
                UPDATE ai_assistant_sessions
                SET title = ?, work_mode = ?, connection_id = ?, dialect = ?, schema_name = ?,
                    focus_tables_json = ?, task_mode = ?, provider_id = ?, model = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    new_title,
                    new_mode,
                    new_cid,
                    new_dialect,
                    new_schema,
                    json.dumps(new_focus, ensure_ascii=False),
                    new_task_mode,
                    new_pid,
                    new_mdl,
                    now,
                    session_id,
                ),
            )
        return self.get_session_detail(session_id)

    def delete_session(self, session_id: str) -> bool:
        sid = (session_id or "").strip()
        if not sid:
            return False
        with self.ctx.store.module_db(scope="private") as conn:
            cur = conn.execute(
                "DELETE FROM ai_assistant_sessions WHERE session_id = ?", (sid,)
            )
            conn.execute("DELETE FROM ai_assistant_messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM ai_assistant_proposals WHERE session_id = ?", (sid,))
        return cur.rowcount > 0

    def _insert_message(
        self,
        *,
        session_id: str,
        turn_index: int,
        role: str,
        task_mode: str = "auto",
        response_type: str = "answer",
        content: str = "",
        steps: Optional[List[Dict[str, Any]]] = None,
        citations: Optional[List[Dict[str, Any]]] = None,
        clarification: Optional[Dict[str, Any]] = None,
        next_steps: Optional[List[Dict[str, str]]] = None,
        result_table: Optional[Dict[str, Any]] = None,
        proposal_id: str = "",
        provider_name: str = "",
        model: str = "",
        total_elapsed_ms: float = 0.0,
    ) -> Dict[str, Any]:
        now = _now_str()
        with self.ctx.store.module_db(scope="private") as conn:
            cur = conn.execute(
                """
                INSERT INTO ai_assistant_messages (
                    session_id, turn_index, role, task_mode, response_type, content,
                    steps_json, citations_json, clarification_json, next_steps_json,
                    result_table_json, proposal_id, provider_name, model, total_elapsed_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    int(turn_index),
                    role,
                    task_mode,
                    response_type,
                    content,
                    json.dumps(steps or [], ensure_ascii=False),
                    json.dumps(citations or [], ensure_ascii=False),
                    json.dumps(clarification or {}, ensure_ascii=False),
                    json.dumps(next_steps or [], ensure_ascii=False),
                    json.dumps(result_table or {}, ensure_ascii=False),
                    proposal_id or "",
                    provider_name or "",
                    model or "",
                    round(float(total_elapsed_ms or 0.0), 1),
                    now,
                ),
            )
            msg_id = int(cur.lastrowid or 0)
            row = conn.execute(
                "SELECT * FROM ai_assistant_messages WHERE id = ?", (msg_id,)
            ).fetchone()
            prop_map: Dict[str, Dict[str, Any]] = {}
            if proposal_id:
                prow = conn.execute(
                    "SELECT * FROM ai_assistant_proposals WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                if prow is not None:
                    prop_map[proposal_id] = self._row_to_proposal_dict(prow)
        return self._row_to_message_dict(row, prop_map)

    def _insert_proposal(
        self,
        *,
        session_id: str,
        connection_id: str,
        dialect: str,
        schema_name: str,
        title: str,
        rationale: str,
        original_sql: str,
        assessment: Dict[str, Any],
        estimated_rows: Optional[int],
    ) -> Dict[str, Any]:
        pid = f"prop_{uuid.uuid4().hex[:12]}"
        now = _now_str()
        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                """
                INSERT INTO ai_assistant_proposals (
                    proposal_id, session_id, message_id, connection_id, dialect, schema_name,
                    title, rationale, original_sql, final_sql, sql_hash, statements_json,
                    risk_level, category, warnings_json, estimated_rows, status,
                    execution_result_json, created_at, decided_at
                ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '{}', ?, '')
                """,
                (
                    pid,
                    session_id,
                    connection_id,
                    dialect,
                    schema_name,
                    title or "待确认执行语句",
                    rationale or "",
                    original_sql,
                    original_sql,
                    str(assessment.get("sql_hash") or ""),
                    json.dumps(assessment.get("statement_risks") or [], ensure_ascii=False),
                    str(assessment.get("risk_level") or "STANDARD_WRITE"),
                    str(assessment.get("category") or "DDL"),
                    json.dumps(assessment.get("warnings") or [], ensure_ascii=False),
                    estimated_rows,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM ai_assistant_proposals WHERE proposal_id = ?", (pid,)
            ).fetchone()
        return self._row_to_proposal_dict(row)

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        pid = (proposal_id or "").strip()
        if not pid:
            return None
        with self.ctx.store.module_db(scope="private") as conn:
            row = conn.execute(
                "SELECT * FROM ai_assistant_proposals WHERE proposal_id = ?", (pid,)
            ).fetchone()
        return self._row_to_proposal_dict(row) if row is not None else None
