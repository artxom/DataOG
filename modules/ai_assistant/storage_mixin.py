"""Data.O.G 智能数据助手 Layer 2 私有会话、消息与写操作提案持久化 Mixin."""

from __future__ import annotations

import datetime
import json
import re
from typing import Any, Dict, List, Optional
import uuid

from modules.ai_assistant.attachments import estimate_tokens, format_with_line_numbers


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
                thinking_mode TEXT NOT NULL DEFAULT 'auto',
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
                ("thinking_mode", "TEXT NOT NULL DEFAULT 'auto'"),
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
                reasoning TEXT NOT NULL DEFAULT '',
                reasoning_ms REAL NOT NULL DEFAULT 0,
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
                ("reasoning", "TEXT NOT NULL DEFAULT ''"),
                ("reasoning_ms", "REAL NOT NULL DEFAULT 0"),
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
                outline_json TEXT NOT NULL DEFAULT '{}',
                analysis_json TEXT NOT NULL DEFAULT '{}',
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
                ("outline_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("analysis_json", "TEXT NOT NULL DEFAULT '{}'"),
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
        store.register_table_schema(
            "ai_assistant_runs",
            """
            CREATE TABLE IF NOT EXISTS ai_assistant_runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                user_message_id INTEGER NOT NULL DEFAULT 0,
                assistant_message_id INTEGER NOT NULL DEFAULT 0,
                run_mode TEXT NOT NULL DEFAULT 'goal',
                status TEXT NOT NULL DEFAULT 'queued',
                request_json TEXT NOT NULL DEFAULT '{}',
                plan_json TEXT NOT NULL DEFAULT '{}',
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                progress_pct INTEGER NOT NULL DEFAULT 0,
                current_stage TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                provider_id TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                thinking_mode TEXT NOT NULL DEFAULT 'auto',
                last_event_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ai_asst_runs_sid
                ON ai_assistant_runs(session_id, created_at DESC);
            """,
            [
                ("session_id", "TEXT NOT NULL DEFAULT ''"),
                ("user_message_id", "INTEGER NOT NULL DEFAULT 0"),
                ("assistant_message_id", "INTEGER NOT NULL DEFAULT 0"),
                ("run_mode", "TEXT NOT NULL DEFAULT 'goal'"),
                ("status", "TEXT NOT NULL DEFAULT 'queued'"),
                ("request_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("plan_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("checkpoint_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("progress_pct", "INTEGER NOT NULL DEFAULT 0"),
                ("current_stage", "TEXT NOT NULL DEFAULT ''"),
                ("error_message", "TEXT NOT NULL DEFAULT ''"),
                ("provider_id", "TEXT NOT NULL DEFAULT ''"),
                ("model", "TEXT NOT NULL DEFAULT ''"),
                ("thinking_mode", "TEXT NOT NULL DEFAULT 'auto'"),
                ("last_event_id", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("finished_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="private",
        )
        store.register_table_schema(
            "ai_assistant_run_events",
            """
            CREATE TABLE IF NOT EXISTS ai_assistant_run_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT 'message',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ai_asst_run_ev_rid
                ON ai_assistant_run_events(run_id, event_id ASC);
            """,
            [
                ("run_id", "TEXT NOT NULL DEFAULT ''"),
                ("event_type", "TEXT NOT NULL DEFAULT 'message'"),
                ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ],
            scope="private",
        )

    @staticmethod
    def _row_to_session_dict(row: Any) -> Dict[str, Any]:
        keys = set(row.keys())
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
            "thinking_mode": str(row["thinking_mode"] or "auto") if "thinking_mode" in keys else "auto",
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
        keys = set(row.keys())
        pid = str(row["proposal_id"] or "")
        att_raw = row["attachments_json"] if "attachments_json" in keys else "[]"
        return {
            "id": int(row["id"]),
            "session_id": str(row["session_id"]),
            "turn_index": int(row["turn_index"] or 0),
            "role": str(row["role"] or "user"),
            "task_mode": str(row["task_mode"] or "auto"),
            "response_type": str(row["response_type"] or "answer"),
            "content": str(row["content"] or ""),
            "reasoning": str(row["reasoning"] or "") if "reasoning" in keys else "",
            "reasoning_ms": float(row["reasoning_ms"] or 0.0) if "reasoning_ms" in keys else 0.0,
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
        keys = set(row.keys())
        outline_raw = row["outline_json"] if "outline_json" in keys else "{}"
        analysis_raw = row["analysis_json"] if "analysis_json" in keys else "{}"
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
            "recommended_strategy": str(row["recommended_strategy"] or "full_context"),
            "summary": str(row["summary"] or ""),
            "csv_profile": _safe_json_loads(row["csv_profile_json"], {}),
            "outline": _safe_json_loads(outline_raw, {}),
            "analysis": _safe_json_loads(analysis_raw, {}),
            "created_at": str(row["created_at"] or ""),
        }
        if include_raw:
            out["raw_text"] = str(row["raw_text"] or "")
        return out

    @staticmethod
    def _row_to_run_dict(row: Any) -> Dict[str, Any]:
        keys = set(row.keys())
        return {
            "run_id": str(row["run_id"]),
            "session_id": str(row["session_id"] or ""),
            "user_message_id": int(row["user_message_id"] or 0),
            "assistant_message_id": int(row["assistant_message_id"] or 0) if "assistant_message_id" in keys else 0,
            "run_mode": str(row["run_mode"] or "goal"),
            "status": str(row["status"] or "queued"),
            "request": _safe_json_loads(row["request_json"], {}) if "request_json" in keys else {},
            "plan": _safe_json_loads(row["plan_json"], {}),
            "checkpoint": _safe_json_loads(row["checkpoint_json"], {}),
            "progress_pct": int(row["progress_pct"] or 0),
            "current_stage": str(row["current_stage"] or ""),
            "error_message": str(row["error_message"] or ""),
            "provider_id": str(row["provider_id"] or ""),
            "model": str(row["model"] or ""),
            "thinking_mode": str(row["thinking_mode"] or "auto"),
            "last_event_id": int(row["last_event_id"] or 0),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
        }

    @staticmethod
    def _row_to_run_event_dict(row: Any) -> Dict[str, Any]:
        return {
            "event_id": int(row["event_id"]),
            "run_id": str(row["run_id"] or ""),
            "event_type": str(row["event_type"] or "message"),
            "payload": _safe_json_loads(row["payload_json"], {}),
            "created_at": str(row["created_at"] or ""),
        }

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
        thinking_mode: str = "auto",
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
        clean_tmode = (thinking_mode or "auto").strip().lower()
        if clean_tmode not in ("auto", "enabled", "disabled"):
            clean_tmode = "auto"

        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                """
                INSERT INTO ai_assistant_sessions (
                    session_id, title, work_mode, connection_id, dialect, schema_name,
                    focus_tables_json, task_mode, provider_id, model, thinking_mode, rolling_summary,
                    turn_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, ?)
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
                    clean_tmode,
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
        thinking_mode: Optional[str] = None,
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
        if thinking_mode is None:
            new_tmode = current.get("thinking_mode") or "auto"
        else:
            cand_tm = thinking_mode.strip().lower()
            new_tmode = cand_tm if cand_tm in ("auto", "enabled", "disabled") else "auto"
        now = _now_str()

        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                """
                UPDATE ai_assistant_sessions
                SET title = ?, work_mode = ?, connection_id = ?, dialect = ?, schema_name = ?,
                    focus_tables_json = ?, task_mode = ?, provider_id = ?, model = ?, thinking_mode = ?, updated_at = ?
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
                    new_tmode,
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
            run_rows = conn.execute(
                "SELECT run_id FROM ai_assistant_runs WHERE session_id = ?",
                (sid,),
            ).fetchall()
            for rr in run_rows:
                conn.execute(
                    "DELETE FROM ai_assistant_run_events WHERE run_id = ?",
                    (str(rr["run_id"]),),
                )
            conn.execute("DELETE FROM ai_assistant_runs WHERE session_id = ?", (sid,))
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
        reasoning: str = "",
        reasoning_ms: float = 0.0,
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
                    reasoning, reasoning_ms,
                    steps_json, citations_json, clarification_json, next_steps_json,
                    result_table_json, attachments_json, proposal_id, provider_name, model,
                    total_elapsed_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    int(turn_index),
                    role,
                    task_mode,
                    response_type,
                    content,
                    reasoning or "",
                    round(float(reasoning_ms or 0.0), 1),
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
                    oversized, recommended_strategy, summary, raw_text, csv_profile_json, outline_json, analysis_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(parsed.get("recommended_strategy") or "full_context"),
                    str(parsed.get("summary") or ""),
                    str(parsed.get("raw_text") or ""),
                    json.dumps(parsed.get("csv_profile") or {}, ensure_ascii=False),
                    json.dumps(parsed.get("outline") or {}, ensure_ascii=False),
                    json.dumps(parsed.get("analysis") or {}, ensure_ascii=False),
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

    def read_attachment_lines(
        self,
        attachment_id: str,
        start_line: int = 1,
        end_line: int = 250,
        *,
        max_lines: int = 400,
        max_tokens: int = 10000,
    ) -> Optional[Dict[str, Any]]:
        """按 1-based 行号区间读取附件原文片段（带行号，受行数与 Token 双重安全上限保护）."""
        aid = (attachment_id or "").strip()
        if not aid:
            return None
        with self.ctx.store.module_db(scope="private") as conn:
            arow = conn.execute(
                "SELECT attachment_id, filename, raw_text FROM ai_assistant_attachments WHERE attachment_id = ?",
                (aid,),
            ).fetchone()
        if arow is None:
            return None

        all_lines = str(arow["raw_text"] or "").splitlines()
        total_lines = len(all_lines)
        if total_lines == 0:
            return {
                "attachment_id": aid,
                "filename": str(arow["filename"] or ""),
                "start_line": 0,
                "end_line": 0,
                "total_lines": 0,
                "has_more": False,
                "truncated_by_tokens": False,
                "content": "",
            }

        s_line = max(1, min(total_lines, int(start_line or 1)))
        req_end = int(end_line or (s_line + 249))
        if req_end < s_line:
            req_end = s_line + 249
        safe_max_lines = max(1, min(400, int(max_lines or 400)))
        e_line = min(total_lines, req_end, s_line + safe_max_lines - 1)

        selected_lines: List[str] = []
        running_tokens = 0
        truncated_by_tokens = False
        safe_max_tokens = max(500, int(max_tokens or 10000))
        actual_end = s_line - 1

        for ln_no in range(s_line, e_line + 1):
            ln_text = all_lines[ln_no - 1]
            ln_tok = estimate_tokens(ln_text) + 2
            if selected_lines and running_tokens + ln_tok > safe_max_tokens:
                truncated_by_tokens = True
                break
            selected_lines.append(ln_text)
            running_tokens += ln_tok
            actual_end = ln_no

        numbered = format_with_line_numbers("\n".join(selected_lines), start_line=s_line)
        return {
            "attachment_id": aid,
            "filename": str(arow["filename"] or ""),
            "start_line": s_line,
            "end_line": actual_end,
            "total_lines": total_lines,
            "has_more": actual_end < total_lines,
            "truncated_by_tokens": truncated_by_tokens,
            "content": numbered,
        }

    def search_attachment_content(
        self,
        attachment_id: str,
        keyword: str,
        *,
        regex: bool = False,
        context_lines: int = 2,
        limit: int = 12,
    ) -> Optional[Dict[str, Any]]:
        aid = (attachment_id or "").strip()
        kw = (keyword or "").strip()
        if not aid:
            return None
        with self.ctx.store.module_db(scope="private") as conn:
            arow = conn.execute(
                "SELECT attachment_id, filename, raw_text FROM ai_assistant_attachments WHERE attachment_id = ?",
                (aid,),
            ).fetchone()
            if arow is None:
                return None
            crows = conn.execute(
                "SELECT chunk_index, title, range_label, start_line, end_line FROM ai_assistant_attachment_chunks WHERE attachment_id = ? ORDER BY chunk_index ASC",
                (aid,),
            ).fetchall()

        all_lines = str(arow["raw_text"] or "").splitlines()
        total_lines = len(all_lines)
        cap = max(1, min(30, int(limit or 12)))
        ctx_n = max(0, min(6, int(context_lines if context_lines is not None else 2)))

        chunks_meta = [
            {
                "chunk_index": int(cr["chunk_index"] or 0),
                "title": str(cr["title"] or ""),
                "range_label": str(cr["range_label"] or ""),
                "start_line": int(cr["start_line"] or 1),
                "end_line": int(cr["end_line"] or total_lines),
            }
            for cr in crows
        ]

        def _locate_chunk(ln: int) -> Dict[str, Any]:
            for cm in chunks_meta:
                if cm["start_line"] <= ln <= cm["end_line"]:
                    return cm
            return chunks_meta[0] if chunks_meta else {"chunk_index": 0, "title": "", "range_label": f"L{ln}"}

        def _build_hit(line_no: int, line_text: str) -> Dict[str, Any]:
            cm = _locate_chunk(line_no)
            c_start = max(1, line_no - ctx_n)
            c_end = min(total_lines, line_no + ctx_n)
            ctx_block = format_with_line_numbers(
                "\n".join(all_lines[c_start - 1 : c_end]),
                start_line=c_start,
            )
            return {
                "chunk_index": cm["chunk_index"],
                "title": cm["title"],
                "range_label": cm["range_label"],
                "line_no": line_no,
                "snippet": line_text.strip()[:240],
                "context": ctx_block[:1200],
            }

        hits: List[Dict[str, Any]] = []
        if regex and kw:
            try:
                pat = re.compile(kw, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"正则表达式语法错误: {exc}") from exc
            for idx, line in enumerate(all_lines, start=1):
                if pat.search(line):
                    hits.append(_build_hit(idx, line))
                    if len(hits) >= cap:
                        break
        else:
            terms = [t.lower() for t in kw.split() if t.strip()]
            if terms:
                for idx, line in enumerate(all_lines, start=1):
                    low = line.lower()
                    if all(t in low for t in terms):
                        hits.append(_build_hit(idx, line))
                        if len(hits) >= cap:
                            break
                if not hits:
                    for idx, line in enumerate(all_lines, start=1):
                        low = line.lower()
                        if any(t in low for t in terms):
                            hits.append(_build_hit(idx, line))
                            if len(hits) >= cap:
                                break

        return {
            "attachment_id": aid,
            "filename": str(arow["filename"] or ""),
            "keyword": kw,
            "total_lines": total_lines,
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

    # =========================================================================
    # 后台持久化 Run 与断点续流事件 (ai_assistant_runs / ai_assistant_run_events)
    # =========================================================================

    def create_run(
        self,
        *,
        session_id: str,
        user_message_id: int = 0,
        run_mode: str = "goal",
        request_payload: Optional[Dict[str, Any]] = None,
        provider_id: str = "",
        model: str = "",
        thinking_mode: str = "auto",
    ) -> Dict[str, Any]:
        rid = f"run_{uuid.uuid4().hex[:14]}"
        now = _now_str()
        clean_mode = (run_mode or "goal").strip().lower()
        if clean_mode not in ("standard", "goal"):
            clean_mode = "goal"
        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                """
                INSERT INTO ai_assistant_runs (
                    run_id, session_id, user_message_id, assistant_message_id, run_mode, status,
                    request_json, plan_json, checkpoint_json, progress_pct, current_stage,
                    error_message, provider_id, model, thinking_mode, last_event_id,
                    created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, 0, ?, 'queued', ?, '{}', '{}', 0, '准备中', '', ?, ?, ?, 0, ?, ?, '')
                """,
                (
                    rid,
                    (session_id or "").strip(),
                    int(user_message_id or 0),
                    clean_mode,
                    json.dumps(request_payload or {}, ensure_ascii=False),
                    (provider_id or "").strip(),
                    (model or "").strip(),
                    (thinking_mode or "auto").strip().lower(),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM ai_assistant_runs WHERE run_id = ?", (rid,)
            ).fetchone()
        return self._row_to_run_dict(row)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        rid = (run_id or "").strip()
        if not rid:
            return None
        with self.ctx.store.module_db(scope="private") as conn:
            row = conn.execute(
                "SELECT * FROM ai_assistant_runs WHERE run_id = ?", (rid,)
            ).fetchone()
        return self._row_to_run_dict(row) if row is not None else None

    def get_active_run_for_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        sid = (session_id or "").strip()
        if not sid:
            return None
        with self.ctx.store.module_db(scope="private") as conn:
            row = conn.execute(
                """
                SELECT * FROM ai_assistant_runs
                WHERE session_id = ?
                  AND status IN ('queued', 'probing', 'planning', 'running', 'verifying', 'paused_recoverable', 'failed')
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (sid,),
            ).fetchone()
        return self._row_to_run_dict(row) if row is not None else None

    def update_run(
        self,
        run_id: str,
        *,
        status: Optional[str] = None,
        plan: Optional[Dict[str, Any]] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        request_payload: Optional[Dict[str, Any]] = None,
        progress_pct: Optional[int] = None,
        current_stage: Optional[str] = None,
        error_message: Optional[str] = None,
        assistant_message_id: Optional[int] = None,
        provider_id: Optional[str] = None,
        model: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        rid = (run_id or "").strip()
        if not rid:
            return None
        sets: List[str] = ["updated_at = ?"]
        params: List[Any] = [_now_str()]
        if status is not None:
            sets.append("status = ?")
            params.append(str(status).strip())
        if plan is not None:
            sets.append("plan_json = ?")
            params.append(json.dumps(plan, ensure_ascii=False))
        if checkpoint is not None:
            sets.append("checkpoint_json = ?")
            params.append(json.dumps(checkpoint, ensure_ascii=False))
        if request_payload is not None:
            sets.append("request_json = ?")
            params.append(json.dumps(request_payload, ensure_ascii=False))
        if progress_pct is not None:
            sets.append("progress_pct = ?")
            params.append(max(0, min(100, int(progress_pct))))
        if current_stage is not None:
            sets.append("current_stage = ?")
            params.append(str(current_stage)[:120])
        if error_message is not None:
            sets.append("error_message = ?")
            params.append(str(error_message)[:1000])
        if assistant_message_id is not None:
            sets.append("assistant_message_id = ?")
            params.append(int(assistant_message_id))
        if provider_id is not None:
            sets.append("provider_id = ?")
            params.append(str(provider_id).strip())
        if model is not None:
            sets.append("model = ?")
            params.append(str(model).strip())
        if finished_at is not None:
            sets.append("finished_at = ?")
            params.append(str(finished_at))

        params.append(rid)
        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                f"UPDATE ai_assistant_runs SET {', '.join(sets)} WHERE run_id = ?",
                tuple(params),
            )
            row = conn.execute(
                "SELECT * FROM ai_assistant_runs WHERE run_id = ?", (rid,)
            ).fetchone()
        return self._row_to_run_dict(row) if row is not None else None

    def append_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        rid = (run_id or "").strip()
        ev_t = (event_type or "message").strip()
        now = _now_str()
        ev_payload = dict(payload or {})
        ev_payload.setdefault("type", ev_t)
        ev_payload.setdefault("run_id", rid)
        with self.ctx.store.module_db(scope="private") as conn:
            cur = conn.execute(
                """
                INSERT INTO ai_assistant_run_events (run_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (rid, ev_t, json.dumps(ev_payload, ensure_ascii=False), now),
            )
            ev_id = int(cur.lastrowid or 0)
            conn.execute(
                "UPDATE ai_assistant_runs SET last_event_id = ?, updated_at = ? WHERE run_id = ?",
                (ev_id, now, rid),
            )
        ev_payload["event_id"] = ev_id
        return {
            "event_id": ev_id,
            "run_id": rid,
            "event_type": ev_t,
            "payload": ev_payload,
            "created_at": now,
        }

    def list_run_events(
        self,
        run_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        rid = (run_id or "").strip()
        if not rid:
            return []
        cap = max(1, min(5000, int(limit or 1000)))
        with self.ctx.store.module_db(scope="private") as conn:
            rows = conn.execute(
                """
                SELECT * FROM ai_assistant_run_events
                WHERE run_id = ? AND event_id > ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (rid, max(0, int(after_event_id or 0)), cap),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = self._row_to_run_event_dict(r)
            d["payload"]["event_id"] = d["event_id"]
            out.append(d)
        return out
