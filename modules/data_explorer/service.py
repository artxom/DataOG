"""Data.O.G AI 只读智能数据探查核心服务 (DataExplorerService).

架构与安全红线说明：
1. 插件严格隔离：仅依赖 `core.platform` 底座与 Python 标准库，与其他 `modules/*` 零私有互引；
2. 零新增第三方依赖：直接复用 `LLMHub`（标准库 OpenAI 兼容客户端）、`MetadataHub` 与 `QueryEngine.execute_readonly_sql`；
3. 物理级绝对只读：AI 生成的每一条采样 SQL 与推荐复核 SQL 均强制通过双模态词法白名单沙箱 (`validate_readonly_sql`)，
   且底层执行强制绑定硬件只读会话 (`readonly_session`) 与 `finally` 无条件 `rollback()`；
4. 单机桌面架构说明：Data.O.G 为本地 `127.0.0.1` 单机桌面工具而非多租户公网 SaaS，不涉及用户账户密码哈希、OAuth/MFA 或云端防病毒服务；
   所有本地状态库 (`explorer_sessions` / `explorer_messages`) 均 100% 采用 `?` 参数化预编译 SQL 防注入，错误信息严格脱敏凭据。
"""

from __future__ import annotations

import datetime
import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.platform.context import PlatformContext
from core.platform.llm_hub import extract_json_payload
from core.platform.models import ConnectionProfile, TableMeta
from core.platform.readonly_guard import ReadOnlyViolationError, validate_readonly_sql

MAX_EXPLORE_STEPS_CAP = 8
MAX_SAMPLE_ROWS_CAP = 200
LLM_CONTEXT_ROWS_CAP = 40
STEP_TIMEOUT_SEC = 12
LOOP_WALLCLOCK_BUDGET_SEC = 90.0

_GOAL_GUIDANCE: Dict[str, str] = {
    "profile": (
        "当前探查重心为【数据分布画像】：优先统计总行数、主键/关键业务键去重数、"
        "核心数值字段的 MIN/MAX/AVG/分位区间以及时间分区字段的起止跨度与近期批次量级。"
    ),
    "quality": (
        "当前探查重心为【异常与脏数据排查】：优先检测关键字段 NULL/空串比例、"
        "主键或唯一键重复记录 (GROUP BY ... HAVING COUNT(*) > 1)、负金额/越界比例/异常枚举码值及未来或远古异常日期。"
    ),
    "relation": (
        "当前探查重心为【主外键与跨表关联推断】：结合字段命名与类型识别候选关联键，"
        "通过只读聚合采样验证基数关系 (1:1 / 1:N / M:N) 与 LEFT JOIN 孤儿未匹配率。"
    ),
    "metric": (
        "当前探查重心为【业务指标核对】：明确口径过滤条件与汇总粒度，按维度聚合明细表指标，"
        "并与汇总表/目标口径交叉核对差异值及差异集中的批次或机构。"
    ),
    "general": (
        "请围绕用户的具体探查问题，先用轻量聚合或抽样 SQL 摸清数据事实，再基于真实返回数据给出量化结论。"
    ),
}

_SENSITIVE_KV_RE = re.compile(
    r"(?i)(password|passwd|pwd|api[_-]?key|secret|token)\s*[=:]\s*([^\s,;'\"]+)"
)


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sanitize_error_text(raw_err: Any, secret_values: Optional[List[str]] = None) -> str:
    """脱敏数据库或网关异常文本，杜绝连接口令或密钥回显."""
    text = str(raw_err or "未知错误").strip()
    for sec in secret_values or []:
        if sec and len(sec) >= 3 and sec in text:
            text = text.replace(sec, "******")
    text = _SENSITIVE_KV_RE.sub(r"\1=******", text)
    return text[:500]


def _normalize_focus_tables(raw_list: Optional[List[str]]) -> List[str]:
    seen = set()
    cleaned: List[str] = []
    for item in raw_list or []:
        s = str(item or "").strip()
        if not s or len(s) > 160:
            continue
        key = s.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(s)
        if len(cleaned) >= 24:
            break
    return cleaned


def _split_table_ref(raw_ref: str, default_schema: str) -> Tuple[str, str]:
    s = (raw_ref or "").strip().strip('"`[]')
    if "." in s:
        parts = [p.strip().strip('"`[]') for p in s.split(".", 1)]
        return (parts[0] or default_schema, parts[1])
    return (default_schema, s)


def _extract_search_keywords(prompt: str) -> List[str]:
    tokens: List[str] = []
    for m in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,40}", prompt or ""):
        low = m.lower()
        if low not in {
            "select",
            "from",
            "where",
            "group",
            "order",
            "limit",
            "table",
            "count",
            "null",
            "join",
            "left",
            "inner",
        } and low not in tokens:
            tokens.append(low)
    for zh in re.findall(r"[\u4e00-\u9fff]{2,6}", prompt or ""):
        if zh not in {"探查", "分析", "查询", "检查", "排查", "数据", "字段", "情况", "一下", "帮我", "查看", "统计"}:
            if zh not in tokens:
                tokens.append(zh)
    return tokens[:6]


class DataExplorerService:
    """目标驱动的多轮只读数据探查服务."""

    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """通过平台底座幂等注册探查会话与消息轨迹表."""
        self.ctx.store.register_table_schema(
            "explorer_sessions",
            """
            CREATE TABLE IF NOT EXISTS explorer_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                connection_id TEXT NOT NULL DEFAULT '',
                schema_name TEXT NOT NULL DEFAULT '',
                focus_tables_json TEXT NOT NULL DEFAULT '[]',
                provider_id TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                turn_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_explorer_sessions_cid
                ON explorer_sessions(connection_id, updated_at DESC);
            """,
            [
                ("title", "TEXT NOT NULL DEFAULT ''"),
                ("connection_id", "TEXT NOT NULL DEFAULT ''"),
                ("schema_name", "TEXT NOT NULL DEFAULT ''"),
                ("focus_tables_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("provider_id", "TEXT NOT NULL DEFAULT ''"),
                ("model", "TEXT NOT NULL DEFAULT ''"),
                ("turn_count", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ],
        )
        self.ctx.store.register_table_schema(
            "explorer_messages",
            """
            CREATE TABLE IF NOT EXISTS explorer_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL DEFAULT '',
                turn_index INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'user',
                goal_type TEXT NOT NULL DEFAULT 'general',
                content TEXT NOT NULL DEFAULT '',
                steps_json TEXT NOT NULL DEFAULT '[]',
                findings_json TEXT NOT NULL DEFAULT '[]',
                recommended_sql TEXT NOT NULL DEFAULT '',
                provider_name TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                total_elapsed_ms REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_explorer_messages_sid
                ON explorer_messages(session_id, id ASC);
            """,
            [
                ("turn_index", "INTEGER NOT NULL DEFAULT 0"),
                ("role", "TEXT NOT NULL DEFAULT 'user'"),
                ("goal_type", "TEXT NOT NULL DEFAULT 'general'"),
                ("content", "TEXT NOT NULL DEFAULT ''"),
                ("steps_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("findings_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("recommended_sql", "TEXT NOT NULL DEFAULT ''"),
                ("provider_name", "TEXT NOT NULL DEFAULT ''"),
                ("model", "TEXT NOT NULL DEFAULT ''"),
                ("total_elapsed_ms", "REAL NOT NULL DEFAULT 0"),
                ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ],
        )

    @staticmethod
    def _row_to_session_dict(row: Any) -> Dict[str, Any]:
        try:
            focus_tables = json.loads(row["focus_tables_json"] or "[]")
            if not isinstance(focus_tables, list):
                focus_tables = []
        except Exception:
            focus_tables = []
        return {
            "session_id": str(row["session_id"]),
            "title": str(row["title"] or "新探查会话"),
            "connection_id": str(row["connection_id"] or ""),
            "schema_name": str(row["schema_name"] or ""),
            "focus_tables": [str(x) for x in focus_tables if x],
            "provider_id": str(row["provider_id"] or ""),
            "model": str(row["model"] or ""),
            "turn_count": int(row["turn_count"] or 0),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    @staticmethod
    def _row_to_message_dict(row: Any) -> Dict[str, Any]:
        try:
            steps = json.loads(row["steps_json"] or "[]")
            if not isinstance(steps, list):
                steps = []
        except Exception:
            steps = []
        try:
            findings = json.loads(row["findings_json"] or "[]")
            if not isinstance(findings, list):
                findings = []
        except Exception:
            findings = []
        return {
            "id": int(row["id"]),
            "session_id": str(row["session_id"]),
            "turn_index": int(row["turn_index"] or 0),
            "role": str(row["role"] or "user"),
            "goal_type": str(row["goal_type"] or "general"),
            "content": str(row["content"] or ""),
            "steps": steps,
            "findings": findings,
            "recommended_sql": str(row["recommended_sql"] or ""),
            "provider_name": str(row["provider_name"] or ""),
            "model": str(row["model"] or ""),
            "total_elapsed_ms": round(float(row["total_elapsed_ms"] or 0.0), 1),
            "created_at": str(row["created_at"] or ""),
        }

    # =========================================================================
    # 会话管理 CRUD（全参数化预编译 SQL）
    # =========================================================================

    def list_sessions(
        self, connection_id: Optional[str] = None, limit: int = 60
    ) -> List[Dict[str, Any]]:
        safe_limit = max(1, min(200, int(limit or 60)))
        cid = (connection_id or "").strip()
        with self.ctx.store.module_db() as conn:
            if cid:
                rows = conn.execute(
                    """
                    SELECT * FROM explorer_sessions
                    WHERE connection_id = ?
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT ?
                    """,
                    (cid, safe_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM explorer_sessions
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
        return [self._row_to_session_dict(r) for r in rows]

    def create_session(
        self,
        *,
        connection_id: str,
        schema_name: str = "",
        focus_tables: Optional[List[str]] = None,
        title: str = "",
        provider_id: str = "",
        model: str = "",
    ) -> Dict[str, Any]:
        profile = self.ctx.connection_hub.require_profile(connection_id)
        sid = f"exp-{uuid.uuid4().hex[:12]}"
        now = _now_str()
        clean_focus = _normalize_focus_tables(focus_tables)
        eff_schema = (schema_name or profile.default_schema or "").strip()
        eff_title = (title or "").strip()[:120] or "新探查会话"

        with self.ctx.store.module_db() as conn:
            conn.execute(
                """
                INSERT INTO explorer_sessions (
                    session_id, title, connection_id, schema_name,
                    focus_tables_json, provider_id, model, turn_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    sid,
                    eff_title,
                    profile.connection_id,
                    eff_schema,
                    json.dumps(clean_focus, ensure_ascii=False),
                    (provider_id or "").strip(),
                    (model or "").strip(),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM explorer_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
        return self._row_to_session_dict(row)

    def get_session_detail(self, session_id: str) -> Optional[Dict[str, Any]]:
        sid = (session_id or "").strip()
        if not sid:
            return None
        with self.ctx.store.module_db() as conn:
            s_row = conn.execute(
                "SELECT * FROM explorer_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
            if s_row is None:
                return None
            m_rows = conn.execute(
                "SELECT * FROM explorer_messages WHERE session_id = ? ORDER BY id ASC",
                (sid,),
            ).fetchall()
        session = self._row_to_session_dict(s_row)
        session["messages"] = [self._row_to_message_dict(m) for m in m_rows]
        return session

    def update_session(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        schema_name: Optional[str] = None,
        focus_tables: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        sid = (session_id or "").strip()
        if not sid:
            return None
        with self.ctx.store.module_db() as conn:
            existing = conn.execute(
                "SELECT * FROM explorer_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
            if existing is None:
                return None
            cur = self._row_to_session_dict(existing)
            new_title = cur["title"] if title is None else (title.strip()[:120] or cur["title"])
            new_schema = cur["schema_name"] if schema_name is None else schema_name.strip()
            new_focus = (
                cur["focus_tables"]
                if focus_tables is None
                else _normalize_focus_tables(focus_tables)
            )
            conn.execute(
                """
                UPDATE explorer_sessions
                SET title = ?, schema_name = ?, focus_tables_json = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    new_title,
                    new_schema,
                    json.dumps(new_focus, ensure_ascii=False),
                    _now_str(),
                    sid,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM explorer_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
        return self._row_to_session_dict(updated)

    def delete_session(self, session_id: str) -> bool:
        sid = (session_id or "").strip()
        if not sid:
            return False
        with self.ctx.store.module_db() as conn:
            cur = conn.execute(
                "DELETE FROM explorer_sessions WHERE session_id = ?", (sid,)
            )
            conn.execute(
                "DELETE FROM explorer_messages WHERE session_id = ?", (sid,)
            )
            return bool(cur.rowcount and cur.rowcount > 0)

    # =========================================================================
    # 元数据上下文构建与多步 ReAct 自动采样引擎
    # =========================================================================

    @staticmethod
    def _format_table_meta_brief(meta: TableMeta) -> str:
        fq = f"{meta.schema_name}.{meta.table_name}" if meta.schema_name else meta.table_name
        tags: List[str] = [meta.object_type or "TABLE"]
        if meta.table_comment:
            tags.append(f"注释: {meta.table_comment}")
        if meta.primary_keys:
            tags.append(f"PK: {','.join(meta.primary_keys)}")
        if meta.distribute_keys:
            tags.append(f"分布键: {','.join(meta.distribute_keys)}")
        if meta.partition_keys:
            tags.append(f"分区键: {','.join(meta.partition_keys)}")
        lines = [f"- {fq} ({' | '.join(tags)})"]
        for col in (meta.columns or [])[:60]:
            nn = " NOT NULL" if col.is_not_null else ""
            pk = " [PK]" if col.is_primary_key else ""
            cmt = f" -- {col.column_comment}" if col.column_comment else ""
            lines.append(f"    · {col.column_name}: {col.data_type}{nn}{pk}{cmt}")
        if len(meta.columns or []) > 60:
            lines.append(f"    · ... 其余 {len(meta.columns) - 60} 个字段已省略")
        return "\n".join(lines)

    def _build_metadata_context(
        self,
        profile: ConnectionProfile,
        schema_name: str,
        focus_tables: List[str],
        prompt: str,
    ) -> str:
        cid = profile.connection_id
        eff_schema = (schema_name or profile.default_schema or "").strip()

        try:
            tree = self.ctx.metadata_hub.get_catalog_tree(cid, auto_refresh=True)
        except Exception:
            tree = {"schemas": [], "tables": []}

        detailed_blocks: List[str] = []
        loaded_keys = set()

        # 1. 优先加载用户在左侧 DataGrip 目录树显式勾选的焦点表详情（最多 8 张）
        for raw_ref in focus_tables[:8]:
            s_name, t_name = _split_table_ref(raw_ref, eff_schema)
            if not t_name:
                continue
            dedup_k = f"{s_name.lower()}.{t_name.lower()}"
            if dedup_k in loaded_keys:
                continue
            try:
                t_meta = self.ctx.metadata_hub.get_table_metadata(
                    cid,
                    schema_name=s_name,
                    table_name=t_name,
                    force_refresh=False,
                    penetrate_views=False,
                )
                detailed_blocks.append(self._format_table_meta_brief(t_meta))
                loaded_keys.add(dedup_k)
            except Exception:
                continue

        # 2. 从用户问题关键词自动检索补充关联表结构（最多补充至 8 张）
        if len(detailed_blocks) < 8:
            for kw in _extract_search_keywords(prompt):
                if len(detailed_blocks) >= 8:
                    break
                try:
                    hits = self.ctx.metadata_hub.search_objects(
                        keyword=kw, connection_id=cid, limit=6
                    )
                except Exception:
                    hits = []
                for h in hits:
                    if len(detailed_blocks) >= 8:
                        break
                    hs = str(h.get("schema_name") or eff_schema)
                    ht = str(h.get("table_name") or "")
                    if not ht:
                        continue
                    dk = f"{hs.lower()}.{ht.lower()}"
                    if dk in loaded_keys:
                        continue
                    try:
                        t_meta = self.ctx.metadata_hub.get_table_metadata(
                            cid,
                            schema_name=hs,
                            table_name=ht,
                            force_refresh=False,
                            penetrate_views=False,
                        )
                        detailed_blocks.append(self._format_table_meta_brief(t_meta))
                        loaded_keys.add(dk)
                    except Exception:
                        continue

        # 3. 若未勾选焦点表且关键词未命中，自动采样前 3 张表结构 + 全目录清单摘要
        all_tables = tree.get("tables") or []
        if not detailed_blocks and all_tables:
            for item in all_tables[:3]:
                ts = str(item.get("schema_name") or eff_schema)
                tn = str(item.get("table_name") or "")
                if not tn:
                    continue
                try:
                    t_meta = self.ctx.metadata_hub.get_table_metadata(
                        cid,
                        schema_name=ts,
                        table_name=tn,
                        force_refresh=False,
                        penetrate_views=False,
                    )
                    detailed_blocks.append(self._format_table_meta_brief(t_meta))
                except Exception:
                    continue

        catalog_lines: List[str] = []
        for item in all_tables[:60]:
            ts = str(item.get("schema_name") or "")
            tn = str(item.get("table_name") or "")
            tc = str(item.get("table_comment") or "")
            fq = f"{ts}.{tn}" if ts else tn
            catalog_lines.append(f"{fq} ({tc})" if tc else fq)

        sections = [
            f"目标数据库类型: {profile.db_type} | 默认 Schema: {eff_schema or 'default'}",
        ]
        if focus_tables:
            sections.append(f"用户在左侧数据库目录树勾选的焦点表: {', '.join(focus_tables)}")
        else:
            sections.append("用户未限定单表（支持在已连接库表范围内自动探查）")
        if catalog_lines:
            sections.append(
                f"库表目录概览 (共 {len(all_tables)} 个对象，展示前 {len(catalog_lines)} 个):\n"
                + ", ".join(catalog_lines)
            )
        if detailed_blocks:
            sections.append("重点表字段结构详情:\n" + "\n".join(detailed_blocks))
        return "\n\n".join(sections)

    @staticmethod
    def _build_system_prompt(
        db_type: str,
        goal_type: str,
        max_steps: int,
        sample_limit: int,
        meta_context: str,
    ) -> str:
        goal_hint = _GOAL_GUIDANCE.get(goal_type, _GOAL_GUIDANCE["general"])
        return (
            "你是 Data.O.G 首席数据探查与数据治理分析专家。\n"
            "你工作在 100% 硬件级只读沙箱中，必须基于真实 SQL 采样结果回答问题，严禁凭空编造任何数值或比例。\n\n"
            f"【探查方向指引】\n{goal_hint}\n\n"
            "【绝对只读与 SQL 编写铁律】\n"
            f"1. 目标数据库方言为 `{db_type}`，单轮最多可执行 {max_steps} 步只读采样查询，单步最多返回 {sample_limit} 行。\n"
            "2. 仅允许生成单条纯只读 `SELECT` 或 `WITH ... SELECT` 查询；严禁分号多语句，严禁任何写/DDL/锁/INTO/系统控制函数。\n"
            "3. 优先使用聚合统计（COUNT / COUNT(DISTINCT) / SUM(CASE WHEN ...) / MIN / MAX / GROUP BY）一次性获取高信息密度证据，再按需抽样异常明细。\n"
            "4. SQLite 方言下 `main` 库无需重复库名前缀或使用 `table` 直接访问；PG/DWS/MySQL 请使用准确的 `schema.table` 与真实存在字段。\n\n"
            f"【当前数据源元数据上下文】\n{meta_context}\n\n"
            "【输出格式要求（必须严格输出合法 JSON 对象，不要附加额外解释文本）】\n"
            "- 当你需要执行下一步采样查询时，输出：\n"
            '{"action":"query","purpose":"简明说明本步采样目的（36字以内）","sql":"SELECT ..."}\n'
            "- 当你已获得充足采样依据准备给出最终分析结论时，输出：\n"
            '{"action":"conclude","summary":"核心探查结论摘要（简练、直接列出关键量化数字）",'
            '"findings":[{"tone":"warning|info|success|error","title":"发现项标题","detail":"量化事实与成因建议","step_ref":1}],'
            '"recommended_sql":"可选：供用户在主编辑器复核或导出的单条只读 SELECT 语句（无则留空字符串）"}'
        )

    @staticmethod
    def _format_step_observation_for_llm(step: Dict[str, Any]) -> str:
        step_no = step["step_no"]
        status = step["status"]
        if status == "blocked":
            return (
                f"[Step {step_no} 沙箱硬阻断]\n"
                f"SQL: {step['sql']}\n"
                f"原因: {step['error']}\n"
                "请立即改用合规的纯只读 SELECT 查询，或直接输出 action=\"conclude\"。"
            )
        if status == "error":
            return (
                f"[Step {step_no} SQL 执行报错]\n"
                f"SQL: {step['sql']}\n"
                f"错误: {step['error']}\n"
                "请核对上方元数据中的真实表名、字段名与方言函数修正查询，或基于已有结果输出 action=\"conclude\"。"
            )
        cols = step.get("columns") or []
        rows = (step.get("rows") or [])[:LLM_CONTEXT_ROWS_CAP]
        compact_rows = [
            [
                (str(cell)[:160] + "..." if isinstance(cell, str) and len(cell) > 160 else cell)
                for cell in r
            ]
            for r in rows
        ]
        trunc_note = (
            f"（总返回 {step['row_count']} 行，以下展示前 {len(compact_rows)} 行）"
            if step["row_count"] > len(compact_rows)
            else f"（共 {step['row_count']} 行）"
        )
        return (
            f"[Step {step_no} 采样成功 · {step['elapsed_ms']} ms {trunc_note}]\n"
            f"SQL: {step['sql']}\n"
            f"列头: {json.dumps(cols, ensure_ascii=False)}\n"
            f"行数据: {json.dumps(compact_rows, ensure_ascii=False)}"
        )

    @staticmethod
    def _parse_agent_decision(raw_content: str) -> Dict[str, Any]:
        try:
            parsed = extract_json_payload(raw_content)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        # 若大模型直接输出了总结自然语言而未包裹 JSON，安全兜底为 conclude
        fallback_text = (raw_content or "").strip()
        return {
            "action": "conclude",
            "summary": fallback_text[:1500] or "探查已完成。",
            "findings": [],
            "recommended_sql": "",
        }

    @staticmethod
    def _normalize_findings(raw_findings: Any, max_step: int) -> List[Dict[str, Any]]:
        if not isinstance(raw_findings, list):
            return []
        valid_tones = {"info", "success", "warning", "error"}
        cleaned: List[Dict[str, Any]] = []
        for item in raw_findings[:10]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()[:160]
            detail = str(item.get("detail") or "").strip()[:600]
            if not title and not detail:
                continue
            tone = str(item.get("tone") or "info").strip().lower()
            if tone not in valid_tones:
                tone = "info"
            try:
                step_ref = int(item.get("step_ref") or 0)
            except (TypeError, ValueError):
                step_ref = 0
            if step_ref < 0 or step_ref > max_step:
                step_ref = 0
            cleaned.append(
                {
                    "tone": tone,
                    "title": title or "探查发现",
                    "detail": detail,
                    "step_ref": step_ref,
                }
            )
        return cleaned

    def ask_and_explore(
        self,
        *,
        connection_id: str,
        prompt: str,
        session_id: str = "",
        schema_name: str = "",
        focus_tables: Optional[List[str]] = None,
        goal_type: str = "general",
        provider_id: str = "",
        model: str = "",
        max_steps: int = 5,
        sample_limit: int = 100,
    ) -> Dict[str, Any]:
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ValueError("请输入要探查的目标或问题")

        profile = self.ctx.connection_hub.require_profile(connection_id)
        resolved = self.ctx.llm_hub.resolve_provider_and_model(
            provider_id=provider_id, model=model, require_ready=True
        )
        assert resolved is not None
        llm_prof, eff_model = resolved

        safe_max_steps = max(1, min(MAX_EXPLORE_STEPS_CAP, int(max_steps or 5)))
        safe_sample_limit = max(10, min(MAX_SAMPLE_ROWS_CAP, int(sample_limit or 100)))
        clean_focus = _normalize_focus_tables(focus_tables)
        eff_schema = (schema_name or profile.default_schema or "").strip()
        eff_goal = (goal_type or "general").strip().lower()
        if eff_goal not in _GOAL_GUIDANCE:
            eff_goal = "general"

        # 1. 获取或自动创建探查会话，加载最近历史对话供多轮追问衔接
        existing_detail = self.get_session_detail(session_id) if session_id else None
        if existing_detail is None or existing_detail["connection_id"] != profile.connection_id:
            auto_title = clean_prompt.splitlines()[0].strip()[:36]
            created = self.create_session(
                connection_id=profile.connection_id,
                schema_name=eff_schema,
                focus_tables=clean_focus,
                title=auto_title,
                provider_id=llm_prof.provider_id,
                model=eff_model,
            )
            eff_session_id = created["session_id"]
            prior_messages: List[Dict[str, Any]] = []
            next_turn_idx = 1
        else:
            eff_session_id = existing_detail["session_id"]
            prior_messages = existing_detail.get("messages") or []
            next_turn_idx = int(existing_detail.get("turn_count") or 0) + 1
            if existing_detail.get("title") in ("", "新探查会话") and next_turn_idx == 1:
                self.update_session(
                    eff_session_id,
                    title=clean_prompt.splitlines()[0].strip()[:36],
                    schema_name=eff_schema,
                    focus_tables=clean_focus,
                )
            else:
                self.update_session(
                    eff_session_id,
                    schema_name=eff_schema,
                    focus_tables=clean_focus,
                )

        # 2. 组装元数据上下文与历史轮次摘要
        meta_context = self._build_metadata_context(
            profile, eff_schema, clean_focus, clean_prompt
        )
        sys_prompt = self._build_system_prompt(
            db_type=profile.db_type,
            goal_type=eff_goal,
            max_steps=safe_max_steps,
            sample_limit=safe_sample_limit,
            meta_context=meta_context,
        )

        llm_messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
        for hist in prior_messages[-8:]:
            if hist["role"] == "user":
                llm_messages.append({"role": "user", "content": hist["content"][:1200]})
            else:
                brief_steps = [
                    f"Step {s.get('step_no')}: {s.get('sql')} -> {s.get('row_count', 0)} rows ({s.get('status')})"
                    for s in (hist.get("steps") or [])[:5]
                ]
                hist_digest = hist["content"][:1200]
                if brief_steps:
                    hist_digest = "历史采样:\n" + "\n".join(brief_steps) + "\n结论:\n" + hist_digest
                llm_messages.append({"role": "assistant", "content": hist_digest})

        llm_messages.append({"role": "user", "content": clean_prompt})

        # 3. 执行受控多步只读探查循环
        t_start = time.perf_counter()
        executed_steps: List[Dict[str, Any]] = []
        final_summary = ""
        final_findings: List[Dict[str, Any]] = []
        final_rec_sql = ""
        concluded = False
        seen_sqls = set()
        secrets_to_mask = [s for s in (profile.password, llm_prof.api_key) if s]

        for step_no in range(1, safe_max_steps + 1):
            if (time.perf_counter() - t_start) > LOOP_WALLCLOCK_BUDGET_SEC:
                break

            chat_res = self.ctx.llm_hub.chat_completion(
                messages=llm_messages,
                provider_id=llm_prof.provider_id,
                model=eff_model,
                temperature=0.1,
                timeout_sec=40.0,
            )
            raw_reply = str(chat_res.get("content") or "")
            decision = self._parse_agent_decision(raw_reply)
            action = str(decision.get("action") or "").strip().lower()

            if action == "conclude" or (action != "query" and not decision.get("sql")):
                final_summary = str(
                    decision.get("summary") or decision.get("conclusion") or raw_reply
                ).strip()
                final_findings = self._normalize_findings(
                    decision.get("findings"), len(executed_steps)
                )
                cand_rec = str(decision.get("recommended_sql") or "").strip()
                if cand_rec:
                    try:
                        final_rec_sql = validate_readonly_sql(
                            cand_rec, db_type=profile.db_type
                        )
                    except ReadOnlyViolationError:
                        final_rec_sql = ""
                concluded = True
                break

            cand_sql = str(decision.get("sql") or "").strip()
            purpose = (
                str(decision.get("purpose") or "").strip()[:120]
                or f"执行第 {step_no} 步只读采样查询"
            )
            llm_messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {"action": "query", "purpose": purpose, "sql": cand_sql},
                        ensure_ascii=False,
                    ),
                }
            )

            # 防止模型死循环重复提交完全相同的 SQL
            norm_sql_key = re.sub(r"\s+", " ", cand_sql).strip().lower()
            if norm_sql_key in seen_sqls:
                llm_messages.append(
                    {
                        "role": "user",
                        "content": "该条 SQL 已经执行过并获得采样数据，请勿重复查询；请换一个探查维度，或直接输出 action=\"conclude\" 给出结论。",
                    }
                )
                continue
            seen_sqls.add(norm_sql_key)

            step_t0 = time.perf_counter()
            try:
                q_res = self.ctx.query_engine.execute_readonly_sql(
                    profile.connection_id,
                    cand_sql,
                    limit=safe_sample_limit,
                    max_limit_cap=MAX_SAMPLE_ROWS_CAP,
                    timeout_sec=STEP_TIMEOUT_SEC,
                )
                step_record: Dict[str, Any] = {
                    "step_no": len(executed_steps) + 1,
                    "purpose": purpose,
                    "sql": q_res.sql,
                    "status": "ok",
                    "columns": [c.name for c in q_res.columns],
                    "rows": q_res.rows,
                    "row_count": q_res.row_count,
                    "truncated": bool(q_res.truncated),
                    "elapsed_ms": round(q_res.elapsed_ms, 1),
                    "error": "",
                }
            except ReadOnlyViolationError as vio_exc:
                elapsed = round((time.perf_counter() - step_t0) * 1000.0, 1)
                step_record = {
                    "step_no": len(executed_steps) + 1,
                    "purpose": purpose,
                    "sql": cand_sql[:1500],
                    "status": "blocked",
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                    "truncated": False,
                    "elapsed_ms": elapsed,
                    "error": str(vio_exc),
                }
            except Exception as sql_exc:
                elapsed = round((time.perf_counter() - step_t0) * 1000.0, 1)
                step_record = {
                    "step_no": len(executed_steps) + 1,
                    "purpose": purpose,
                    "sql": cand_sql[:1500],
                    "status": "error",
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                    "truncated": False,
                    "elapsed_ms": elapsed,
                    "error": _sanitize_error_text(sql_exc, secrets_to_mask),
                }

            executed_steps.append(step_record)
            obs_text = self._format_step_observation_for_llm(step_record)
            llm_messages.append({"role": "user", "content": obs_text})

        # 4. 若触达步数或墙钟熔断阈值仍未 conclude，发起一次强制收敛总结
        if not concluded:
            llm_messages.append(
                {
                    "role": "user",
                    "content": (
                        "已达到本轮最大只读采样步数上限。请严禁再输出 action=\"query\"，"
                        "立即基于上方已获取的真实采样数据输出 action=\"conclude\" 的完整 JSON 分析结论。"
                    ),
                }
            )
            try:
                synth_res = self.ctx.llm_hub.chat_completion(
                    messages=llm_messages,
                    provider_id=llm_prof.provider_id,
                    model=eff_model,
                    temperature=0.1,
                    timeout_sec=35.0,
                )
                synth_dec = self._parse_agent_decision(str(synth_res.get("content") or ""))
                final_summary = str(
                    synth_dec.get("summary")
                    or synth_dec.get("conclusion")
                    or synth_res.get("content")
                    or ""
                ).strip()
                final_findings = self._normalize_findings(
                    synth_dec.get("findings"), len(executed_steps)
                )
                cand_rec = str(synth_dec.get("recommended_sql") or "").strip()
                if cand_rec:
                    try:
                        final_rec_sql = validate_readonly_sql(
                            cand_rec, db_type=profile.db_type
                        )
                    except ReadOnlyViolationError:
                        final_rec_sql = ""
            except Exception as synth_exc:
                ok_cnt = sum(1 for s in executed_steps if s["status"] == "ok")
                final_summary = (
                    f"已完成 {len(executed_steps)} 步只读采样（成功 {ok_cnt} 步），"
                    f"最终汇总生成超时或异常（{_sanitize_error_text(synth_exc, secrets_to_mask)}），可展开上方采样步骤查看真实数据。"
                )

        if not final_summary:
            ok_cnt = sum(1 for s in executed_steps if s["status"] == "ok")
            final_summary = f"已完成 {len(executed_steps)} 步只读探查采样（成功 {ok_cnt} 步）。"

        # 若未显式生成 recommended_sql，默认回填最后一条成功执行的只读采样 SQL 方便用户一键填入编辑器
        if not final_rec_sql:
            for st in reversed(executed_steps):
                if st["status"] == "ok" and st.get("sql"):
                    final_rec_sql = str(st["sql"])
                    break

        total_elapsed_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
        now = _now_str()

        # 5. 原子持久化本轮用户提问与 AI 探查轨迹/结论
        with self.ctx.store.module_db() as conn:
            conn.execute(
                """
                INSERT INTO explorer_messages (
                    session_id, turn_index, role, goal_type, content,
                    steps_json, findings_json, recommended_sql,
                    provider_name, model, total_elapsed_ms, created_at
                ) VALUES (?, ?, 'user', ?, ?, '[]', '[]', '', '', '', 0, ?)
                """,
                (eff_session_id, next_turn_idx, eff_goal, clean_prompt, now),
            )
            cur_asst = conn.execute(
                """
                INSERT INTO explorer_messages (
                    session_id, turn_index, role, goal_type, content,
                    steps_json, findings_json, recommended_sql,
                    provider_name, model, total_elapsed_ms, created_at
                ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    eff_session_id,
                    next_turn_idx,
                    eff_goal,
                    final_summary,
                    json.dumps(executed_steps, ensure_ascii=False),
                    json.dumps(final_findings, ensure_ascii=False),
                    final_rec_sql,
                    llm_prof.name,
                    eff_model,
                    total_elapsed_ms,
                    now,
                ),
            )
            asst_msg_id = int(cur_asst.lastrowid or 0)
            conn.execute(
                """
                UPDATE explorer_sessions
                SET schema_name = ?,
                    focus_tables_json = ?,
                    provider_id = ?,
                    model = ?,
                    turn_count = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    eff_schema,
                    json.dumps(clean_focus, ensure_ascii=False),
                    llm_prof.provider_id,
                    eff_model,
                    next_turn_idx,
                    now,
                    eff_session_id,
                ),
            )
            asst_row = conn.execute(
                "SELECT * FROM explorer_messages WHERE id = ?", (asst_msg_id,)
            ).fetchone()

        self.ctx.store.add_log(
            "INFO",
            f"AI探查 [{profile.name}]",
            f"完成第 {next_turn_idx} 轮只读探查（共 {len(executed_steps)} 步采样，模型 {eff_model}，耗时 {total_elapsed_ms} ms）",
        )

        detail = self.get_session_detail(eff_session_id)
        return {
            "session": detail,
            "assistant_message": self._row_to_message_dict(asst_row) if asst_row else None,
            "sessions": self.list_sessions(connection_id=profile.connection_id),
        }
