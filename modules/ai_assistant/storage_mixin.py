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
                attachments_json TEXT NOT NULL DEFAULT '[]',
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
                ("attachments_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("proposal_id", "TEXT NOT NULL DEFAULT ''"),
                ("provider_name", "TEXT NOT NULL DEFAULT ''"),
                ("model", "TEXT NOT NULL DEFAULT ''"),
                ("total_elapsed_ms", "REAL NOT NULL DEFAULT 0"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="private",
        )
        store.register_table_schema(
            "ai_assistant_attachments",
            """
            CREATE TABLE IF NOT EXISTS ai_assistant_attachments (
                attachment_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL DEFAULT '',
                file_ext TEXT NOT NULL DEFAULT '',
                file_kind TEXT NOT NULL DEFAULT 'text',
                encoding TEXT NOT NULL DEFAULT 'utf-8',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                line_count INTEGER NOT NULL DEFAULT 0,
                row_count INTEGER NOT NULL DEFAULT 0,
                estimated_tokens INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                oversized INTEGER NOT NULL DEFAULT 0,
                recommended_strategy TEXT NOT NULL DEFAULT 'inline',
                summary TEXT NOT NULL DEFAULT '',
                raw_text TEXT NOT NULL DEFAULT '',
                csv_profile_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ai_asst_att_sid
                ON ai_assistant_attachments(session_id, created_at ASC);
            """,
            [
                ("session_id", "TEXT NOT NULL DEFAULT ''"),
                ("filename", "TEXT NOT NULL DEFAULT ''"),
                ("file_ext", "TEXT NOT NULL DEFAULT ''"),
                ("file_kind", "TEXT NOT NULL DEFAULT 'text'"),
                ("encoding", "TEXT NOT NULL DEFAULT 'utf-8'"),
                ("size_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("line_count", "INTEGER NOT NULL DEFAULT 0"),
                ("row_count", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("chunk_count", "INTEGER NOT NULL DEFAULT 0"),
                ("oversized", "INTEGER NOT NULL DEFAULT 0"),
                ("recommended_strategy", "TEXT NOT NULL DEFAULT 'inline'"),
                ("summary", "TEXT NOT NULL DEFAULT ''"),
                ("raw_text", "TEXT NOT NULL DEFAULT ''"),
                ("csv_profile_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="private",
        )
        store.register_table_schema(
            "ai_assistant_attachment_chunks",
            """
            CREATE TABLE IF NOT EXISTS ai_assistant_attachment_chunks (
                chunk_id TEXT PRIMARY KEY,
                attachment_id TEXT NOT NULL DEFAULT '',
                chunk_index INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                range_label TEXT NOT NULL DEFAULT '',
                start_line INTEGER NOT NULL DEFAULT 0,
                end_line INTEGER NOT NULL DEFAULT 0,
                estimated_tokens INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ai_asst_att_chk_aid
                ON ai_assistant_attachment_chunks(attachment_id, chunk_index ASC);
            """,
            [
                ("attachment_id", "TEXT NOT NULL DEFAULT ''"),
                ("chunk_index", "INTEGER NOT NULL DEFAULT 0"),
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("range_label", "TEXT NOT NULL DEFAULT ''"),
                ("start_line", "INTEGER NOT NULL DEFAULT 0"),
                ("end_line", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("content", "TEXT NOT NULL DEFAULT ''"),
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
        att_raw = row["attachments_json"] if "attachments_json" in row.keys() else "[]"
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
            "attachments": _safe_json_loads(att_raw, []),
            "proposal_id": pid,
            "proposal": proposals_by_id.get(pid) if pid else None,
            "provider_name": str(row["provider_name"] or ""),
            "model": str(row["model"] or ""),
            "total_elapsed_ms": float(row["total_elapsed_ms"] or 0.0),
            "created_at": str(row["created_at"] or ""),
        }

    @staticmethod
    def _row_to_attachment_dict(row: Any, *, include_raw: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "attachment_id": str(row["attachment_id"]),
            "session_id": str(row["session_id"] or ""),
            "filename": str(row["filename"] or ""),
            "file_ext": str(row["file_ext"] or ""),
            "file_kind": str(row["file_kind"] or "text"),
            "encoding": str(row["encoding"] or "utf-8"),
            "size_bytes": int(row["size_bytes"] or 0),
            "line_count": int(row["line_count"] or 0),
            "row_count": int(row["row_count"] or 0),
            "estimated_tokens": int(row["estimated_tokens"] or 0),
            "chunk_count": int(row["chunk_count"] or 0),
            "oversized": bool(row["oversized"]),
            "recommended_strategy": str(row["recommended_strategy"] or "inline"),
            "summary": str(row["summary"] or ""),
            "csv_profile": _safe_json_loads(row["csv_profile_json"], {}),
            "created_at": str(row["created_at"] or ""),
        }
        if include_raw:
            out["raw_text"] = str(row["raw_text"] or "")
        return out

    @staticmethod
    def _row_to_attachment_chunk_dict(row: Any, *, include_content: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "chunk_id": str(row["chunk_id"]),
            "attachment_id": str(row["attachment_id"]),
            "chunk_index": int(row["chunk_index"] or 0),
            "title": str(row["title"] or ""),
            "range_label": str(row["range_label"] or ""),
            "start_line": int(row["start_line"] or 0),
            "end_line": int(row["end_line"] or 0),
            "estimated_tokens": int(row["estimated_tokens"] or 0),
        }
        if include_content:
            d["content"] = str(row["content"] or "")
        return d

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
        session_dict["attachments"] = self.list_session_attachments(
            sid, include_raw=False, include_chunks=False
        )
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
            att_rows = conn.execute(
                "SELECT attachment_id FROM ai_assistant_attachments WHERE session_id = ?",
                (sid,),
            ).fetchall()
            for ar in att_rows:
                conn.execute(
                    "DELETE FROM ai_assistant_attachment_chunks WHERE attachment_id = ?",
                    (str(ar["attachment_id"]),),
                )
            conn.execute("DELETE FROM ai_assistant_attachments WHERE session_id = ?", (sid,))
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
        attachments: Optional[List[Dict[str, Any]]] = None,
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
                    result_table_json, attachments_json, proposal_id, provider_name, model,
                    total_elapsed_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(attachments or [], ensure_ascii=False),
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

    def save_attachment(self, parsed: Dict[str, Any], *, session_id: str = "") -> Dict[str, Any]:
        aid = f"att_{uuid.uuid4().hex[:12]}"
        now = _now_str()
        clean_sid = (session_id or "").strip()
        cutoff = (datetime.datetime.now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        chunks = list(parsed.get("chunks") or [])

        with self.ctx.store.module_db(scope="private") as conn:
            # 惰性清理超过 24 小时未发送绑定的孤立草稿附件
            stale_rows = conn.execute(
                "SELECT attachment_id FROM ai_assistant_attachments WHERE session_id = '' AND created_at < ?",
                (cutoff,),
            ).fetchall()
            for sr in stale_rows:
                stale_id = str(sr["attachment_id"])
                conn.execute(
                    "DELETE FROM ai_assistant_attachment_chunks WHERE attachment_id = ?",
                    (stale_id,),
                )
                conn.execute(
                    "DELETE FROM ai_assistant_attachments WHERE attachment_id = ?",
                    (stale_id,),
                )

            conn.execute(
                """
                INSERT INTO ai_assistant_attachments (
                    attachment_id, session_id, filename, file_ext, file_kind, encoding,
                    size_bytes, line_count, row_count, estimated_tokens, chunk_count,
                    oversized, recommended_strategy, summary, raw_text, csv_profile_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid,
                    clean_sid,
                    str(parsed.get("filename") or "attachment.txt"),
                    str(parsed.get("file_ext") or ".txt"),
                    str(parsed.get("file_kind") or "text"),
                    str(parsed.get("encoding") or "utf-8"),
                    int(parsed.get("size_bytes") or 0),
                    int(parsed.get("line_count") or 0),
                    int(parsed.get("row_count") or 0),
                    int(parsed.get("estimated_tokens") or 0),
                    len(chunks),
                    1 if parsed.get("oversized") else 0,
                    str(parsed.get("recommended_strategy") or "inline"),
                    str(parsed.get("summary") or ""),
                    str(parsed.get("raw_text") or ""),
                    json.dumps(parsed.get("csv_profile") or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO ai_assistant_attachment_chunks (
                    chunk_id, attachment_id, chunk_index, title, range_label,
                    start_line, end_line, estimated_tokens, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"{aid}::c{int(ch.get('chunk_index', idx)):04d}",
                        aid,
                        int(ch.get("chunk_index", idx)),
                        str(ch.get("title") or ""),
                        str(ch.get("range_label") or ""),
                        int(ch.get("start_line") or 0),
                        int(ch.get("end_line") or 0),
                        int(ch.get("estimated_tokens") or 0),
                        str(ch.get("content") or ""),
                    )
                    for idx, ch in enumerate(chunks)
                ],
            )
        return self.get_attachment(aid, include_raw=False, include_chunks=True) or {}

    def get_attachment(
        self,
        attachment_id: str,
        *,
        include_raw: bool = False,
        include_chunks: bool = False,
        include_chunk_content: bool = True,
    ) -> Optional[Dict[str, Any]]:
        aid = (attachment_id or "").strip()
        if not aid:
            return None
        with self.ctx.store.module_db(scope="private") as conn:
            row = conn.execute(
                "SELECT * FROM ai_assistant_attachments WHERE attachment_id = ?",
                (aid,),
            ).fetchone()
            if row is None:
                return None
            item = self._row_to_attachment_dict(row, include_raw=include_raw)
            if include_chunks:
                crows = conn.execute(
                    "SELECT * FROM ai_assistant_attachment_chunks WHERE attachment_id = ? ORDER BY chunk_index ASC",
                    (aid,),
                ).fetchall()
                item["chunks"] = [
                    self._row_to_attachment_chunk_dict(cr, include_content=include_chunk_content)
                    for cr in crows
                ]
        return item

    def list_session_attachments(
        self,
        session_id: str,
        *,
        include_raw: bool = False,
        include_chunks: bool = False,
    ) -> List[Dict[str, Any]]:
        sid = (session_id or "").strip()
        if not sid:
            return []
        with self.ctx.store.module_db(scope="private") as conn:
            rows = conn.execute(
                "SELECT * FROM ai_assistant_attachments WHERE session_id = ? ORDER BY created_at ASC",
                (sid,),
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                att = self._row_to_attachment_dict(r, include_raw=include_raw)
                if include_chunks:
                    crows = conn.execute(
                        "SELECT * FROM ai_assistant_attachment_chunks WHERE attachment_id = ? ORDER BY chunk_index ASC",
                        (att["attachment_id"],),
                    ).fetchall()
                    att["chunks"] = [
                        self._row_to_attachment_chunk_dict(cr, include_content=False)
                        for cr in crows
                    ]
                out.append(att)
        return out

    def bind_attachments_to_session(self, attachment_ids: List[str], session_id: str) -> None:
        sid = (session_id or "").strip()
        clean_ids = [str(x).strip() for x in (attachment_ids or []) if str(x).strip()]
        if not sid or not clean_ids:
            return
        with self.ctx.store.module_db(scope="private") as conn:
            for aid in clean_ids:
                conn.execute(
                    """
                    UPDATE ai_assistant_attachments
                    SET session_id = ?
                    WHERE attachment_id = ? AND (session_id = '' OR session_id = ?)
                    """,
                    (sid, aid, sid),
                )

    def get_attachment_chunk(
        self,
        attachment_id: str,
        chunk_index: int,
        *,
        window: int = 0,
    ) -> Optional[Dict[str, Any]]:
        aid = (attachment_id or "").strip()
        if not aid:
            return None
        win = max(0, min(3, int(window or 0)))
        target_idx = int(chunk_index)
        with self.ctx.store.module_db(scope="private") as conn:
            arow = conn.execute(
                "SELECT * FROM ai_assistant_attachments WHERE attachment_id = ?",
                (aid,),
            ).fetchone()
            if arow is None:
                return None
            crows = conn.execute(
                """
                SELECT * FROM ai_assistant_attachment_chunks
                WHERE attachment_id = ? AND chunk_index BETWEEN ? AND ?
                ORDER BY chunk_index ASC
                """,
                (aid, target_idx - win, target_idx + win),
            ).fetchall()
        if not crows:
            return None
        return {
            "attachment": self._row_to_attachment_dict(arow, include_raw=False),
            "target_chunk_index": target_idx,
            "chunks": [self._row_to_attachment_chunk_dict(cr, include_content=True) for cr in crows],
        }

    def search_attachment_content(
        self,
        attachment_id: str,
        keyword: str,
        *,
        limit: int = 8,
    ) -> Optional[Dict[str, Any]]:
        aid = (attachment_id or "").strip()
        kw = (keyword or "").strip()
        if not aid:
            return None
        with self.ctx.store.module_db(scope="private") as conn:
            arow = conn.execute(
                "SELECT * FROM ai_assistant_attachments WHERE attachment_id = ?",
                (aid,),
            ).fetchone()
            if arow is None:
                return None
            crows = conn.execute(
                "SELECT * FROM ai_assistant_attachment_chunks WHERE attachment_id = ? ORDER BY chunk_index ASC",
                (aid,),
            ).fetchall()

        terms = [t.lower() for t in kw.split() if t.strip()]
        cap = max(1, min(20, int(limit or 8)))
        hits: List[Dict[str, Any]] = []
        for cr in crows:
            content = str(cr["content"] or "")
            start_ln = int(cr["start_line"] or 1)
            for rel_idx, line in enumerate(content.splitlines()):
                low = line.lower()
                if terms and all(t in low for t in terms):
                    hits.append(
                        {
                            "chunk_index": int(cr["chunk_index"] or 0),
                            "title": str(cr["title"] or ""),
                            "range_label": str(cr["range_label"] or ""),
                            "line_no": start_ln + rel_idx,
                            "snippet": line.strip()[:220],
                        }
                    )
                    break
            if len(hits) >= cap:
                break

        # 若整行 AND 未命中，按任意词兜底返回包含任一关键词的片段
        if not hits and terms:
            for cr in crows:
                content = str(cr["content"] or "")
                start_ln = int(cr["start_line"] or 1)
                for rel_idx, line in enumerate(content.splitlines()):
                    low = line.lower()
                    if any(t in low for t in terms):
                        hits.append(
                            {
                                "chunk_index": int(cr["chunk_index"] or 0),
                                "title": str(cr["title"] or ""),
                                "range_label": str(cr["range_label"] or ""),
                                "line_no": start_ln + rel_idx,
                                "snippet": line.strip()[:220],
                            }
                        )
                        break
                if len(hits) >= cap:
                    break

        return {
            "attachment_id": aid,
            "filename": str(arow["filename"] or ""),
            "keyword": kw,
            "hit_count": len(hits),
            "hits": hits,
        }

    def delete_attachment(self, attachment_id: str) -> bool:
        aid = (attachment_id or "").strip()
        if not aid:
            return False
        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                "DELETE FROM ai_assistant_attachment_chunks WHERE attachment_id = ?",
                (aid,),
            )
            cur = conn.execute(
                "DELETE FROM ai_assistant_attachments WHERE attachment_id = ?",
                (aid,),
            )
        return cur.rowcount > 0
