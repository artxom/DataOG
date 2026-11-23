"""Data.O.G v3.0 智能数据助手核心服务 (AiAssistantService).

核心能力：
1. 双模式运行：在线连库模式（元数据/血缘 + 只读 SQL + EXPLAIN + 受控写确认）与离线纯方言模式；
2. 循序渐进多步探索引擎：意图路由 -> 智能澄清门禁 -> 工具循环（查目录/查表结构/查离线知识库/只读 SQL 报错自纠错/EXPLAIN 执行计划）-> 溯源引用 + 下一步推进芯片；
3. 硬安全门禁：自主探索阶段物理仅可访问 `execute_readonly_sql`；所有 DDL 与 DML 写操作只能通过服务端独立词法分级提案 (`STANDARD_WRITE` / `HIGH_RISK`) 展示于对话确认卡片，用户显式确认（高危需输 `CONFIRM`）后才通过 `execute_sql` 逐条执行并自动写回会话与审计日志。
"""

from __future__ import annotations

import datetime
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.platform.builtin_dialect_kb import DIALECT_LABEL_MAP, normalize_kb_dialect
from core.platform.context import PlatformContext
from core.platform.import_progress import ProgressCallback, noop_progress
from core.platform.llm_hub import extract_json_payload
from core.platform.models import ConnectionProfile
from core.platform.sql_risk_guard import classify_sql_batch
from modules.ai_assistant.storage_mixin import (
    AssistantStorageMixin,
    _normalize_focus_tables,
)
from modules.ai_assistant.tools import (
    AssistantToolbox,
    _format_table_meta_detailed,
    _split_table_ref,
    sanitize_error_text,
)

LOOP_WALLCLOCK_BUDGET_SEC = 120.0
MAX_STEPS_CAP = 10

_TASK_MODE_GUIDANCE: Dict[str, str] = {
    "query": (
        "【当前任务重心：智能数据查询】\n"
        "- 先核对焦点表或检索相关库表结构 (`inspect_table` / `search_catalog`)，确认准确字段名与分区过滤条件。\n"
        "- 优先使用聚合或带 `LIMIT` 的只读查询；若 SQL 报错，请阅读报错信息结合知识库自我修正并重试（最多尝试修复）。\n"
        "- 返回数据时提供清晰业务解读，并在结果中保留最终可复用的 SQL。"
    ),
    "troubleshoot": (
        "【当前任务重心：SQL 报错排障】\n"
        "- 优先提取用户报错信息中的错误码（如 `ORA-xxxxx`、`ERROR 1064`、`distribution key`、`SQLCODE`）或异常语法，调用 `search_knowledge` 检索对应方言排查手册。\n"
        "- 在线连库模式下，若涉及真实表/字段，调用 `inspect_table` 核实字段类型/空值/分布键；必要时用 `run_readonly_sql` 验证修复后的只读查询。\n"
        "- 按「根因定位 → 依据溯源 → 修复后 SQL / 安全方案」渐进输出。"
    ),
    "optimize": (
        "【当前任务重心：SQL 性能优化与索引建议】\n"
        "- 先调用 `inspect_table` 检查表的主键、索引、分布键（如 DWS）、分区键及列存设置。\n"
        "- 在线连库模式下对目标 SELECT 语句调用 `explain_sql` 获取真实执行计划（禁止猜测），同时检索知识库调优规则。\n"
        "- 给出瓶颈定位（如全表扫描/隐式强转/重分布 Broadcast）与等价改写 SQL；若建议新增索引，通过 `propose_write_sql` 生成待确认 DDL。"
    ),
    "syntax": (
        "【当前任务重心：方言语法确认与跨库改写】\n"
        "- 严格以当前目标数据库方言（如 DWS/PGSQL/MySQL/Oracle/DM8/Kingbase/Inceptor/DB2/GBase/SQLite）为准。\n"
        "- 先调用 `search_knowledge`（可配合 `read_knowledge_section`）检索该方言官方文档或速查卡，严禁混入其他数据库方言函数或保留字。\n"
        "- 若用户在做跨方言迁移，明确列出逐项语法/函数/数据类型差异与改写后完整 SQL。"
    ),
    "ddl": (
        "【当前任务重心：DDL 建模与受控数据变更】\n"
        "- 写操作或 DDL（CREATE/ALTER/COMMENT/INSERT/UPDATE/DELETE/DROP/TRUNCATE）**绝不可自行执行**。\n"
        "- 若涉及现有表变更，先用 `inspect_table` 检查原表结构并结合目标方言规范生成完整、规范的 SQL。\n"
        "- 必须调用 `propose_write_sql` 或直接输出 `propose_write` 动作，将语句提交至前端二次确认卡片由用户审阅后执行。"
    ),
    "general": (
        "【当前任务重心：综合数据库研发与运维助手】\n"
        "- 根据用户意图灵活组合库表探查、知识库检索、只读 SQL/EXPLAIN 验证与受控变更提案。\n"
        "- 若信息不足或存在多义性（如多张相似表、业务口径不明确），使用 `clarify` 动作给出结构化可选项，不盲猜。"
    ),
}


class ConfirmTokenRequiredError(ValueError):
    """高危写操作缺少 CONFIRM 确认词时抛出的结构化异常."""

    def __init__(self, message: str, assessment: Dict[str, Any]) -> None:
        super().__init__(message)
        self.assessment = assessment


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def infer_task_mode(prompt: str, requested_mode: str = "auto") -> str:
    req = (requested_mode or "auto").strip().lower()
    if req in ("query", "troubleshoot", "optimize", "syntax", "ddl"):
        return req
    text = (prompt or "").lower()
    if any(
        kw in text
        for kw in (
            "报错",
            "错误",
            "失败",
            "异常",
            "ora-",
            "sqlstate",
            "syntax error",
            "error ",
            "为什么跑不通",
            "排障",
        )
    ):
        return "troubleshoot"
    if any(
        kw in text
        for kw in (
            "慢",
            "优化",
            "性能",
            "索引",
            "explain",
            "执行计划",
            "倾斜",
            "全表扫描",
            "耗时",
        )
    ):
        return "optimize"
    if any(
        kw in text
        for kw in (
            "建表",
            "ddl",
            "alter ",
            "create ",
            "drop ",
            "truncate ",
            "insert ",
            "update ",
            "delete ",
            "加字段",
            "改表",
            "插入数据",
            "删除数据",
            "更新数据",
        )
    ):
        return "ddl"
    if any(
        kw in text
        for kw in (
            "语法",
            "支不支持",
            "怎么写",
            "方言",
            "函数",
            "改写",
            "兼容",
            "区别",
            "迁移",
        )
    ):
        return "syntax"
    if any(kw in text for kw in ("查一下", "查询", "多少", "统计", "汇总", "明细", "前10", "top")):
        return "query"
    return "general"


class AiAssistantService(AssistantStorageMixin):
    """Data.O.G v3.0 多轮渐进式数据库智能助手核心服务."""

    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx
        self.ensure_schema(ctx.store)

    def _resolve_scope(
        self,
        *,
        work_mode: str,
        connection_id: str,
        dialect: str,
        schema_name: str,
    ) -> Tuple[str, Optional[ConnectionProfile], str, str]:
        req_mode = (work_mode or "auto").strip().lower()
        cid = (connection_id or "").strip()
        prof: Optional[ConnectionProfile] = None
        if cid and req_mode != "dialect":
            prof = self.ctx.connection_hub.get_profile(cid)
        if prof is not None:
            eff_mode = "connection"
            eff_dialect = prof.db_type
            eff_schema = (schema_name or "").strip() or prof.effective_default_schema()
        else:
            eff_mode = "dialect"
            eff_dialect = normalize_kb_dialect(dialect or "DWS")
            eff_schema = (schema_name or "").strip()
        return eff_mode, prof, eff_dialect, eff_schema

    def _build_context_block(
        self,
        *,
        work_mode: str,
        profile: Optional[ConnectionProfile],
        dialect: str,
        default_schema: str,
        focus_tables: List[str],
    ) -> str:
        sections: List[str] = []
        dialect_label = DIALECT_LABEL_MAP.get(dialect, dialect)
        if profile is not None:
            sections.append(
                f"工作模式: 在线连库模式 | 目标连接: {profile.name} (ID={profile.connection_id}) "
                f"| 数据库方言: {dialect} ({dialect_label}) | 默认 Schema: {default_schema or '-'}"
            )
        else:
            sections.append(
                f"工作模式: 离线纯方言模式（未绑定实时数据库，不可执行查库或 EXPLAIN）"
                f" | 目标方言: {dialect} ({dialect_label})"
            )

        # 全局可用连接拓扑摘要（供感知全局数据源）
        all_conns = self.ctx.connection_hub.list_profiles()
        if all_conns:
            conn_briefs = [
                f"{c['name']} [{c['db_type']}] (id={c['connection_id']})"
                for c in all_conns[:12]
            ]
            sections.append("平台已配置的全局数据源连接: " + "；".join(conn_briefs))

        # 知识库文档目录摘要（尤其高亮用户上传资料）
        user_docs = self.ctx.knowledge_hub.list_documents(
            dialect=dialect, include_builtin=False
        )
        generic_user_docs = (
            self.ctx.knowledge_hub.list_documents(dialect="GENERIC", include_builtin=False)
            if dialect != "GENERIC"
            else []
        )
        combined_user_docs = user_docs + [
            d for d in generic_user_docs if d["doc_id"] not in {u["doc_id"] for u in user_docs}
        ]
        if combined_user_docs:
            doc_lines = [
                f"- 《{d['title']}》 [{d['dialect']}] ({d['chunk_count']}段): {d['summary'][:90]}"
                for d in combined_user_docs[:15]
            ]
            sections.append(
                "用户上传的本地参考知识库文档（可用 search_knowledge 精准检索）:\n"
                + "\n".join(doc_lines)
            )

        cheatsheet = self.ctx.knowledge_hub.get_dialect_cheatsheet_brief(
            dialect, max_chars=1400
        )
        if cheatsheet:
            sections.append(f"当前方言速查基线:\n{cheatsheet}")

        # 在线模式附带焦点表详细元数据与目录概览
        if profile is not None:
            detailed_blocks: List[str] = []
            for tref in focus_tables[:10]:
                sch, tbl = _split_table_ref(tref, default_schema)
                if not tbl:
                    continue
                try:
                    meta = self.ctx.metadata_hub.get_table_metadata(
                        profile.connection_id,
                        schema_name=sch,
                        table_name=tbl,
                        force_refresh=False,
                        penetrate_views=True,
                    )
                    if meta.exists:
                        detailed_blocks.append(_format_table_meta_detailed(meta))
                except Exception:
                    continue
            if detailed_blocks:
                sections.append("用户指定的焦点表详细结构:\n\n" + "\n\n".join(detailed_blocks))

            tree = self.ctx.metadata_hub.get_catalog_tree(
                profile.connection_id, auto_refresh=False
            )
            all_tables = tree.get("tables") or []
            if all_tables:
                cat_items = []
                for item in all_tables[:50]:
                    ts = str(item.get("schema_name") or "")
                    tn = str(item.get("table_name") or "")
                    tc = str(item.get("table_comment") or "")
                    fq = f"{ts}.{tn}" if ts else tn
                    cat_items.append(f"{fq}({tc})" if tc else fq)
                sections.append(
                    f"当前连接库表目录概览 (共 {len(all_tables)} 个对象，展示前 {len(cat_items)} 个):\n"
                    + ", ".join(cat_items)
                )

        return "\n\n".join(sections)

    @staticmethod
    def _build_system_prompt(
        *,
        work_mode: str,
        dialect: str,
        task_mode: str,
        max_steps: int,
        sample_limit: int,
        context_block: str,
    ) -> str:
        guidance = _TASK_MODE_GUIDANCE.get(task_mode, _TASK_MODE_GUIDANCE["general"])
        online_rules = (
            "1. 当前为【在线连库模式】，你只能通过 `run_readonly_sql` 与 `explain_sql` 在物理只读沙箱中探查；"
            "任何 DDL（CREATE/ALTER/DROP/TRUNCATE/COMMENT）或 DML 写操作（INSERT/UPDATE/DELETE/MERGE）**严禁直接执行**，"
            "必须通过 `propose_write_sql` 工具或 `propose_write` 动作提交给用户在对话提示框中二次确认。\n"
            if work_mode == "connection"
            else "1. 当前为【离线纯方言模式】，未连接真实数据库，请勿虚构真实表行数或执行计划；"
            "聚焦方言语法规则、知识库检索与标准 SQL 编写；若需产出 DDL/写语句，使用 `propose_write` 卡片规范呈现以便用户审阅或后续连库执行。\n"
        )
        return (
            "你是 Data.O.G 首席数据库研发与运维专家助手。\n"
            "你具备循序渐进的自主探索与知识库检索能力：先核查事实与官方文档，再下结论，绝不凭空编造表名、字段名或错误码解释。\n\n"
            f"{guidance}\n\n"
            "【安全与行为铁律】\n"
            f"{online_rules}"
            f"2. 目标数据库方言为 `{dialect}`，单轮最多可调用 {max_steps} 次工具，只读查询单步上限 {sample_limit} 行。\n"
            "3. 当用户问题存在关键歧义（例如存在多张候选表不确定查哪张、聚合口径不明确、或未明确目标场景）时，"
            "优先输出 `clarify` 动作给出 2~4 个可点选选项向用户确认，而不是胡乱猜测。\n"
            "4. 若只读 SQL 报错，不要直接放弃，请查看报错信息并结合 `inspect_table` / `search_knowledge` 自我修正重试。\n"
            "5. 引用知识库内容时，请在 `citations` 数组列出实际检索到的 `chunk_id`。\n\n"
            "【可用工具清单（通过 action=\"tool\" 调用）】\n"
            "- `search_catalog`: {\"keyword\":\"关键字\",\"schema_name\":\"可选\",\"limit\":30} — 检索已连接数据库中的表与视图\n"
            "- `inspect_table`: {\"table_ref\":\"schema.table\",\"force_refresh\":false} — 查看字段类型、注释、主键、索引、分布键、视图血缘与 DDL\n"
            "- `search_knowledge`: {\"query\":\"错误码或语法关键词\",\"dialect\":\"" + dialect + "\",\"top_k\":5} — 检索离线参考资料与方言速查卡\n"
            "- `read_knowledge_section`: {\"chunk_id\":\"命中片段ID\",\"window\":1} — 深入读取知识库章节上下文与目录\n"
            "- `run_readonly_sql`: {\"sql\":\"SELECT ...\",\"limit\":100} — 在只读沙箱中执行单条 SELECT/WITH/SHOW/DESC 语句\n"
            "- `explain_sql`: {\"sql\":\"SELECT ...\"} — 查看只读查询真实执行计划（严禁 EXPLAIN ANALYZE）\n"
            "- `propose_write_sql`: {\"title\":\"变更标题\",\"rationale\":\"原因与影响\",\"sql\":\"CREATE/ALTER/INSERT/UPDATE/DELETE...\",\"target_schema\":\"可选\"} — 提交 DDL/写操作待确认提案（不执行）\n\n"
            f"【当前全局上下文】\n{context_block}\n\n"
            "【严格 JSON 输出协议（每次回复必须仅输出单个合法 JSON 对象，禁止包裹多余闲聊文本）】\n"
            "1) 调用工具继续探索：\n"
            '{"action":"tool","tool":"工具名","args":{...},"purpose":"简短说明本步探索意图(36字内)"}\n'
            "2) 存在歧义需用户先选择确认：\n"
            '{"action":"clarify","question":"需要向用户澄清的问题","reason":"为什么需要确认","options":[{"label":"选项标题","detail":"简短说明","value":"回传给AI的选择值"}]}\n'
            "3) 提交 DDL 或数据修改提案由用户二次确认执行：\n"
            '{"action":"propose_write","title":"变更摘要标题","rationale":"变更说明、语法依据与风险提示","target_schema":"可选Schema","sql":"完整规范 SQL（支持分号分隔多条）","summary":"简短导语","next_steps":[{"label":"下一步建议","prompt":"对应追问"}]}\n'
            "4) 完成探索并给出最终结论：\n"
            '{"action":"answer","summary":"核心结论一句话摘要","content":"详细 Markdown 解答（含分点分析、SQL 代码块、调优/修复建议）","citations":["chunk_id_1"],"next_steps":[{"label":"下一步深入按钮","prompt":"点击后自动填入或发送的追问"}]}\n'
        )

    @staticmethod
    def _build_conversation_messages(
        session: Dict[str, Any],
        current_prompt: str,
        clarification_reply: Optional[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        msgs: List[Dict[str, str]] = []
        rolling = str(session.get("rolling_summary") or "").strip()
        if rolling:
            msgs.append(
                {
                    "role": "system",
                    "content": f"【较早轮次对话摘要】\n{rolling}",
                }
            )
        history = list(session.get("messages") or [])
        for m in history[-12:]:
            role = m.get("role", "user")
            if role == "user":
                msgs.append({"role": "user", "content": str(m.get("content") or "")})
            else:
                parts: List[str] = []
                if m.get("content"):
                    parts.append(str(m["content"])[:2000])
                prop = m.get("proposal")
                if isinstance(prop, dict):
                    parts.append(
                        f"[提案 {prop.get('proposal_id')} | 状态={prop.get('status')} | 风险={prop.get('risk_level')}]\n"
                        f"SQL:\n{str(prop.get('final_sql') or prop.get('original_sql') or '')[:1200]}"
                    )
                    exec_res = prop.get("execution_result") or {}
                    if exec_res:
                        parts.append(
                            f"[执行反馈]: {json.dumps(exec_res, ensure_ascii=False)[:1000]}"
                        )
                clar = m.get("clarification") or {}
                if isinstance(clar, dict) and clar.get("question"):
                    parts.append(f"[我曾请求澄清]: {clar['question']}")
                if parts:
                    msgs.append({"role": "assistant", "content": "\n".join(parts)})

        user_text = current_prompt.strip()
        if clarification_reply:
            sel_label = str(clarification_reply.get("label") or clarification_reply.get("value") or "").strip()
            extra_note = str(clarification_reply.get("note") or "").strip()
            if sel_label:
                user_text = (
                    f"{user_text}\n\n[用户澄清选择]: {sel_label}"
                    + (f"（补充说明: {extra_note}）" if extra_note else "")
                )
        msgs.append({"role": "user", "content": user_text})
        return msgs

    def _compact_rolling_summary_if_needed(self, session_id: str) -> None:
        sess = self.get_session_detail(session_id)
        if not sess:
            return
        msgs = sess.get("messages") or []
        if len(msgs) < 16:
            return
        # 纯本地确定性滚动压缩，零额外 LLM 开销
        older = msgs[:-10]
        snippets: List[str] = []
        for m in older[-10:]:
            prefix = "用户" if m.get("role") == "user" else "助手"
            txt = re.sub(r"\s+", " ", str(m.get("content") or "")).strip()[:140]
            if txt:
                snippets.append(f"- {prefix}: {txt}")
        summary_text = "\n".join(snippets)[:1600]
        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                "UPDATE ai_assistant_sessions SET rolling_summary = ? WHERE session_id = ?",
                (summary_text, session_id),
            )

    @staticmethod
    def _parse_agent_payload(raw_text: str) -> Tuple[Optional[Dict[str, Any]], str]:
        try:
            obj = extract_json_payload(raw_text)
            if isinstance(obj, dict):
                return obj, ""
        except Exception as exc:
            return None, str(exc)
        return None, "返回 JSON 不是对象结构"

    @staticmethod
    def _normalize_next_steps(raw_steps: Any) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        if not isinstance(raw_steps, list):
            return out
        for it in raw_steps[:3]:
            if isinstance(it, dict):
                lbl = str(it.get("label") or "").strip()[:36]
                prm = str(it.get("prompt") or lbl).strip()[:300]
                if lbl and prm:
                    out.append({"label": lbl, "prompt": prm})
            elif isinstance(it, str) and it.strip():
                s = it.strip()
                out.append({"label": s[:32], "prompt": s[:300]})
        return out

    @staticmethod
    def _resolve_citations(
        raw_citations: Any,
        toolbox: AssistantToolbox,
    ) -> List[Dict[str, Any]]:
        selected: List[Dict[str, Any]] = []
        seen: set[str] = set()
        if isinstance(raw_citations, list):
            for item in raw_citations:
                cid = (
                    str(item.get("chunk_id") or "")
                    if isinstance(item, dict)
                    else str(item or "")
                ).strip()
                if cid and cid in toolbox.citations_map and cid not in seen:
                    seen.add(cid)
                    selected.append(toolbox.citations_map[cid])
        if not selected and toolbox.citations_map:
            for cid, cdict in list(toolbox.citations_map.items())[:4]:
                selected.append(cdict)
        return selected[:6]

    def chat_turn(
        self,
        *,
        session_id: Optional[str] = None,
        prompt: str,
        work_mode: str = "auto",
        connection_id: str = "",
        dialect: str = "",
        schema_name: str = "",
        focus_tables: Optional[List[str]] = None,
        task_mode: str = "auto",
        provider_id: str = "",
        model: str = "",
        max_steps: int = 6,
        sample_limit: int = 100,
        clarification_reply: Optional[Dict[str, Any]] = None,
        progress_cb: Optional[ProgressCallback] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ValueError("请输入要咨询或探查的数据库问题")

        cb = progress_cb or noop_progress
        t_start = time.perf_counter()

        # 1. 创建或加载会话
        sess = self.get_session_detail(session_id or "") if session_id else None
        if sess is None:
            eff_mode, prof, eff_dialect, eff_schema = self._resolve_scope(
                work_mode=work_mode,
                connection_id=connection_id,
                dialect=dialect,
                schema_name=schema_name,
            )
            sess = self.create_session(
                title=clean_prompt[:32],
                work_mode=eff_mode,
                connection_id=prof.connection_id if prof else "",
                dialect=eff_dialect,
                schema_name=eff_schema,
                focus_tables=focus_tables,
                task_mode=task_mode,
                provider_id=provider_id,
                model=model,
            )
            sid = sess["session_id"]
        else:
            sid = sess["session_id"]
            eff_mode, prof, eff_dialect, eff_schema = self._resolve_scope(
                work_mode=work_mode if work_mode != "auto" else sess["work_mode"],
                connection_id=connection_id if connection_id else (sess["connection_id"] if work_mode != "dialect" else ""),
                dialect=dialect or sess["dialect"],
                schema_name=schema_name or sess["schema_name"],
            )
            eff_focus = (
                _normalize_focus_tables(focus_tables)
                if focus_tables is not None and len(focus_tables) > 0
                else sess["focus_tables"]
            )
            sess = (
                self.update_session(
                    sid,
                    work_mode=eff_mode,
                    connection_id=prof.connection_id if prof else "",
                    dialect=eff_dialect,
                    schema_name=eff_schema,
                    focus_tables=eff_focus,
                    task_mode=task_mode if task_mode != "auto" else sess["task_mode"],
                    provider_id=provider_id or sess["provider_id"],
                    model=model or sess["model"],
                )
                or sess
            )

        eff_focus = sess.get("focus_tables") or []
        eff_task_mode = infer_task_mode(
            clean_prompt,
            task_mode if task_mode != "auto" else sess.get("task_mode", "auto"),
        )
        eff_provider_id = (provider_id or sess.get("provider_id") or "").strip()
        eff_model = (model or sess.get("model") or "").strip()
        safe_max_steps = max(1, min(MAX_STEPS_CAP, int(max_steps or 6)))
        safe_sample_limit = max(10, min(200, int(sample_limit or 100)))

        # 先验大模型可用性（失败不污染会话记录）
        self.ctx.llm_hub.resolve_provider_and_model(
            provider_id=eff_provider_id, model=eff_model, require_ready=True
        )

        cb(8, "准备上下文", f"正在组装 {eff_dialect} 语法基线与元数据上下文...", 0, safe_max_steps)
        context_block = self._build_context_block(
            work_mode=eff_mode,
            profile=prof,
            dialect=eff_dialect,
            default_schema=eff_schema,
            focus_tables=eff_focus,
        )
        system_prompt = self._build_system_prompt(
            work_mode=eff_mode,
            dialect=eff_dialect,
            task_mode=eff_task_mode,
            max_steps=safe_max_steps,
            sample_limit=safe_sample_limit,
            context_block=context_block,
        )

        next_turn = int(sess.get("turn_count") or 0) + 1
        user_msg = self._insert_message(
            session_id=sid,
            turn_index=next_turn,
            role="user",
            task_mode=eff_task_mode,
            response_type="prompt",
            content=clean_prompt,
        )

        toolbox = AssistantToolbox(
            ctx=self.ctx,
            work_mode=eff_mode,
            profile=prof,
            dialect=eff_dialect,
            default_schema=eff_schema,
            sample_limit=safe_sample_limit,
        )

        convo = self._build_conversation_messages(sess, clean_prompt, clarification_reply)
        llm_messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}] + convo

        trace_steps: List[Dict[str, Any]] = []
        used_provider_name = ""
        used_model = ""
        final_obj: Optional[Dict[str, Any]] = None
        fallback_plain_text = ""

        # 2. 多轮循序渐进工具推理循环
        for step_idx in range(1, safe_max_steps + 1):
            if cancel_check and cancel_check():
                raise RuntimeError("用户已取消本次智能会话任务")
            if (time.perf_counter() - t_start) > LOOP_WALLCLOCK_BUDGET_SEC:
                break

            pct = min(85, 15 + int((step_idx - 1) / max(1, safe_max_steps) * 65))
            cb(pct, "智能推理中", f"步骤 {step_idx}/{safe_max_steps} · 正在分析与规划下一步...", step_idx - 1, safe_max_steps)

            llm_res = self.ctx.llm_hub.chat_completion(
                messages=llm_messages,
                provider_id=eff_provider_id,
                model=eff_model,
                temperature=0.1,
                timeout_sec=50.0,
            )
            used_provider_name = str(llm_res.get("provider_name") or "")
            used_model = str(llm_res.get("model") or "")
            raw_reply = str(llm_res.get("content") or "")

            parsed, parse_err = self._parse_agent_payload(raw_reply)
            if parsed is None:
                # 若第一次返回非 JSON，给予一次温和纠偏机会，不立刻失败
                if step_idx == 1 and len(raw_reply.strip()) > 0:
                    fallback_plain_text = raw_reply.strip()
                    llm_messages.append({"role": "assistant", "content": raw_reply})
                    llm_messages.append(
                        {
                            "role": "user",
                            "content": f"格式纠偏提示：上一步未能解析为 JSON ({parse_err})。请严格仅输出一个合法 JSON 对象。",
                        }
                    )
                    continue
                fallback_plain_text = raw_reply.strip()
                break

            action = str(parsed.get("action") or "").strip().lower()
            if action == "conclude":
                action = "answer"
            elif action == "query" and parsed.get("sql"):
                parsed = {
                    "action": "tool",
                    "tool": "run_readonly_sql",
                    "args": {"sql": parsed.get("sql")},
                    "purpose": parsed.get("purpose") or "执行只读查询",
                }
                action = "tool"

            if action == "tool":
                tool_name = str(parsed.get("tool") or "").strip()
                tool_args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
                purpose = str(parsed.get("purpose") or tool_name).strip()[:80]
                cb(
                    min(88, pct + 6),
                    "执行探索工具",
                    f"步骤 {step_idx}/{safe_max_steps} · {purpose}",
                    step_idx,
                    safe_max_steps,
                )
                t_tool = time.perf_counter()
                tool_out = toolbox.dispatch(tool_name, tool_args)
                tool_ms = round((time.perf_counter() - t_tool) * 1000.0, 1)

                trace_steps.append(
                    {
                        "step": len(trace_steps) + 1,
                        "tool": tool_name,
                        "purpose": purpose,
                        "args": tool_args,
                        "ok": bool(tool_out.get("ok")),
                        "elapsed_ms": tool_ms,
                        "summary": str(
                            tool_out.get("note")
                            or tool_out.get("error")
                            or (f"命中 {tool_out.get('hit_count')} 条知识" if "hit_count" in tool_out else "")
                            or (f"发现 {tool_out.get('count')} 个对象" if "count" in tool_out else "")
                            or "完成"
                        )[:180],
                    }
                )
                llm_messages.append({"role": "assistant", "content": raw_reply})
                llm_messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"【工具 `{tool_name}` 返回结果】\n"
                            + json.dumps(tool_out, ensure_ascii=False)[:6500]
                        ),
                    }
                )
                continue

            if action in ("clarify", "propose_write", "answer"):
                final_obj = parsed
                final_obj["action"] = action
                break

            fallback_plain_text = raw_reply.strip()
            break

        # 若用尽工具步数仍未输出终结动作，追加一次强制汇总（禁止继续调工具）
        if final_obj is None and trace_steps:
            cb(90, "归纳结论", "正在基于已收集的探索证据汇总最终回复...", safe_max_steps, safe_max_steps)
            llm_messages.append(
                {
                    "role": "user",
                    "content": (
                        "探索步数已达上限，请立即停止调用工具，直接输出 `action=\"answer\"` 或 `action=\"propose_write\"` 的最终 JSON 结论。"
                    ),
                }
            )
            synth = self.ctx.llm_hub.chat_completion(
                messages=llm_messages,
                provider_id=eff_provider_id,
                model=eff_model,
                temperature=0.1,
                timeout_sec=45.0,
            )
            used_provider_name = str(synth.get("provider_name") or used_provider_name)
            used_model = str(synth.get("model") or used_model)
            parsed_synth, _ = self._parse_agent_payload(str(synth.get("content") or ""))
            if parsed_synth:
                final_obj = parsed_synth
            else:
                fallback_plain_text = str(synth.get("content") or fallback_plain_text).strip()

        # 3. 处理最终响应并落盘
        total_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
        response_type = "answer"
        content_md = ""
        clarification_payload: Dict[str, Any] = {}
        next_steps_list: List[Dict[str, str]] = []
        proposal_id = ""
        citations_list: List[Dict[str, Any]] = []

        if final_obj is None:
            content_md = fallback_plain_text or "已完成分析，但未能得到完整结构化输出，请尝试缩小问题范围或重试。"
            citations_list = self._resolve_citations([], toolbox)
        else:
            act = str(final_obj.get("action") or "answer").lower()
            citations_list = self._resolve_citations(final_obj.get("citations"), toolbox)
            next_steps_list = self._normalize_next_steps(final_obj.get("next_steps"))

            if act == "clarify":
                response_type = "clarify"
                q_text = str(final_obj.get("question") or "请先确认以下信息：").strip()
                reason_text = str(final_obj.get("reason") or "").strip()
                opts: List[Dict[str, str]] = []
                for op in (final_obj.get("options") or [])[:6]:
                    if isinstance(op, dict):
                        lbl = str(op.get("label") or op.get("value") or "").strip()
                        if lbl:
                            opts.append(
                                {
                                    "label": lbl[:80],
                                    "detail": str(op.get("detail") or "").strip()[:160],
                                    "value": str(op.get("value") or lbl).strip()[:160],
                                }
                            )
                    elif isinstance(op, str) and op.strip():
                        opts.append({"label": op.strip()[:80], "detail": "", "value": op.strip()[:160]})
                clarification_payload = {
                    "question": q_text,
                    "reason": reason_text,
                    "options": opts,
                }
                content_md = f"{q_text}" + (f"\n\n> {reason_text}" if reason_text else "")

            elif act == "propose_write":
                sql_candidate = str(final_obj.get("sql") or "").strip()
                if sql_candidate:
                    prop_res = toolbox.tool_propose_write_sql(
                        {
                            "sql": sql_candidate,
                            "title": final_obj.get("title") or "待确认变更语句",
                            "rationale": final_obj.get("rationale") or final_obj.get("summary") or "",
                            "target_schema": final_obj.get("target_schema") or eff_schema,
                        }
                    )
                    if not prop_res.get("ok") and not toolbox.pending_proposal:
                        # 若 LLM 误把纯只读 SELECT 放进了 propose_write，降级展示普通回答
                        content_md = (
                            str(final_obj.get("summary") or "")
                            + f"\n\n```sql\n{sql_candidate}\n```\n\n> 提示：{prop_res.get('error', '')}"
                        ).strip()
                summary_txt = str(final_obj.get("summary") or "").strip()
                rationale_txt = str(final_obj.get("rationale") or "").strip()
                content_md = "\n\n".join(x for x in (summary_txt, rationale_txt) if x) or "已生成待确认 SQL 语句，请在下方卡片核对后决定是否执行。"

            else:
                summary_txt = str(final_obj.get("summary") or "").strip()
                body_txt = str(final_obj.get("content") or "").strip()
                if summary_txt and body_txt and summary_txt not in body_txt:
                    content_md = f"**{summary_txt}**\n\n{body_txt}"
                else:
                    content_md = body_txt or summary_txt or fallback_plain_text or "处理完成。"

        # 若工具阶段生成了待确认写提案，绑定到当前回复消息
        if toolbox.pending_proposal is not None:
            pp = toolbox.pending_proposal
            saved_prop = self._insert_proposal(
                session_id=sid,
                connection_id=pp["connection_id"],
                dialect=pp["dialect"],
                schema_name=pp["schema_name"],
                title=pp["title"],
                rationale=pp["rationale"],
                original_sql=pp["original_sql"],
                assessment=pp["assessment"],
                estimated_rows=pp["estimated_rows"],
            )
            proposal_id = saved_prop["proposal_id"]
            response_type = "proposal"

        # 组装结果表（避免大结果塞爆 SQLite：仅留前 200 行）
        result_table = toolbox.latest_result_table or {}
        if result_table and isinstance(result_table.get("rows"), list):
            result_table["rows"] = result_table["rows"][:200]

        assistant_msg = self._insert_message(
            session_id=sid,
            turn_index=next_turn,
            role="assistant",
            task_mode=eff_task_mode,
            response_type=response_type,
            content=content_md,
            steps=trace_steps,
            citations=citations_list,
            clarification=clarification_payload,
            next_steps=next_steps_list,
            result_table=result_table,
            proposal_id=proposal_id,
            provider_name=used_provider_name,
            model=used_model,
            total_elapsed_ms=total_ms,
        )

        # 回填 proposal.message_id 与更新会话元数据
        now = _now_str()
        with self.ctx.store.module_db(scope="private") as conn:
            if proposal_id:
                conn.execute(
                    "UPDATE ai_assistant_proposals SET message_id = ? WHERE proposal_id = ?",
                    (int(assistant_msg["id"]), proposal_id),
                )
            conn.execute(
                """
                UPDATE ai_assistant_sessions
                SET turn_count = ?, updated_at = ?, title = CASE WHEN turn_count = 0 THEN ? ELSE title END
                WHERE session_id = ?
                """,
                (next_turn, now, clean_prompt[:36], sid),
            )

        self._compact_rolling_summary_if_needed(sid)
        cb(100, "回答就绪", f"已完成多步分析与响应（耗时 {total_ms:.0f} ms）", safe_max_steps, safe_max_steps)

        updated_session = self.get_session_detail(sid)
        return {
            "session": updated_session,
            "user_message": user_msg,
            "assistant_message": assistant_msg,
        }

    # =========================================================================
    # 写操作与 DDL 分级二次确认执行门禁 (Confirm / Reject)
    # =========================================================================

    def confirm_proposal(
        self,
        proposal_id: str,
        *,
        edited_sql: Optional[str] = None,
        confirm_token: str = "",
        connection_id: Optional[str] = None,
        note: str = "",
    ) -> Dict[str, Any]:
        prop = self.get_proposal(proposal_id)
        if prop is None:
            raise KeyError("未找到指定的待确认执行提案")
        if prop["status"] != "pending":
            raise ValueError(
                f"该提案当前状态为 `{prop['status']}`，已处理完毕，禁止重复执行"
            )

        # 确定目标连接：若提案已绑定连接则强制锁定该连接；仅纯方言模式生成的无绑定提案允许确认时指定目标连接
        locked_cid = (prop.get("connection_id") or "").strip()
        req_cid = (connection_id or "").strip()
        if locked_cid and req_cid and locked_cid != req_cid:
            raise ValueError("该变更提案已锁定生成时的目标连接，不允许跨连接改道执行")
        target_cid = locked_cid or req_cid
        if not target_cid:
            raise ValueError("当前提案未绑定数据源连接，请先选择要执行的目标数据库连接")

        profile = self.ctx.connection_hub.require_profile(target_cid)
        secret_vals = [profile.password] if profile.password else []

        candidate_sql = (
            edited_sql.strip()
            if edited_sql is not None and edited_sql.strip()
            else prop["original_sql"]
        )
        # 无论前端传了什么，服务端一律独立重新做词法切分与分级定级！
        assessment = classify_sql_batch(candidate_sql, db_type=profile.db_type)
        if assessment.risk_level == "READONLY":
            raise ValueError(
                "修改后的语句属于纯只读查询，无需通过写操作确认通道执行"
            )

        if assessment.requires_confirm_token and (confirm_token or "").strip().upper() != "CONFIRM":
            raise ConfirmTokenRequiredError(
                "检测到高危变更语句（包含 DROP / TRUNCATE / DELETE / UPDATE / 权限操作或结构删除），请输入确认词 CONFIRM 后再执行。",
                assessment.to_dict(),
            )

        stmt_results: List[Dict[str, Any]] = []
        all_ok = True
        has_ddl = False
        for idx, stmt in enumerate(assessment.statements):
            r_meta = assessment.statement_risks[idx]
            if r_meta.category == "DDL":
                has_ddl = True
            t0 = time.perf_counter()
            try:
                qres = self.ctx.query_engine.execute_sql(
                    target_cid,
                    stmt,
                    limit=200,
                    timeout_sec=60,
                )
                stmt_results.append(
                    {
                        "index": r_meta.index,
                        "sql": stmt,
                        "primary_verb": r_meta.primary_verb,
                        "category": r_meta.category,
                        "ok": True,
                        "affected_rows": int(qres.affected_rows or 0),
                        "row_count": int(qres.row_count or 0),
                        "elapsed_ms": round(float(qres.elapsed_ms or 0.0), 1),
                        "message": qres.message,
                        "error": "",
                    }
                )
            except Exception as exc:
                all_ok = False
                err_msg = sanitize_error_text(exc, secret_vals)
                stmt_results.append(
                    {
                        "index": r_meta.index,
                        "sql": stmt,
                        "primary_verb": r_meta.primary_verb,
                        "category": r_meta.category,
                        "ok": False,
                        "affected_rows": 0,
                        "row_count": 0,
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                        "message": "",
                        "error": err_msg,
                    }
                )
                break

        succeeded_cnt = sum(1 for r in stmt_results if r["ok"])
        final_status = "executed" if all_ok else "failed"
        decided_at = _now_str()
        exec_summary = {
            "connection_id": target_cid,
            "connection_name": profile.name,
            "db_type": profile.db_type,
            "all_ok": all_ok,
            "succeeded_count": succeeded_cnt,
            "total_count": len(assessment.statements),
            "executed_at": decided_at,
            "note": (note or "").strip()[:200],
            "statements": stmt_results,
        }

        final_formatted_sql = ";\n\n".join(assessment.statements) + ";"
        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                """
                UPDATE ai_assistant_proposals
                SET connection_id = ?, dialect = ?, final_sql = ?, sql_hash = ?,
                    statements_json = ?, risk_level = ?, category = ?, warnings_json = ?,
                    status = ?, execution_result_json = ?, decided_at = ?
                WHERE proposal_id = ?
                """,
                (
                    target_cid,
                    profile.db_type,
                    final_formatted_sql,
                    assessment.sql_hash,
                    json.dumps(
                        [r.to_dict() for r in assessment.statement_risks],
                        ensure_ascii=False,
                    ),
                    assessment.risk_level,
                    assessment.category,
                    json.dumps(assessment.warnings, ensure_ascii=False),
                    final_status,
                    json.dumps(exec_summary, ensure_ascii=False),
                    decided_at,
                    proposal_id,
                ),
            )

        self.ctx.store.add_log(
            "INFO" if all_ok else "ERROR",
            f"AI助手二次确认执行 [{profile.name}]",
            (
                f"提案 {proposal_id} ({assessment.risk_level}) 执行完成: {succeeded_cnt}/{len(assessment.statements)} 条成功"
                if all_ok
                else f"提案 {proposal_id} ({assessment.risk_level}) 执行中止: 第 {len(stmt_results)} 条失败 - {stmt_results[-1]['error']}"
            ),
            sql_text=final_formatted_sql[:2000],
        )

        # DDL 成功后自动刷新目标连接元数据缓存，确保后续提问立即可见新表/新字段
        catalog_refreshed = False
        if all_ok and has_ddl:
            try:
                self.ctx.metadata_hub.invalidate_negative_cache(target_cid)
                self.ctx.metadata_hub.refresh_catalog(target_cid)
                catalog_refreshed = True
            except Exception:
                catalog_refreshed = False

        # 回写一条执行结果消息进入对话历史，让后续多轮对话感知执行结果或直接诊断修复
        sess = self.get_session_detail(prop["session_id"])
        turn_idx = int((sess or {}).get("turn_count") or 0)
        if all_ok:
            msg_lines = [
                f"✅ **已确认执行成功**（目标连接 `{profile.name}` · `{profile.db_type}` · {succeeded_cnt}/{len(assessment.statements)} 条语句）"
            ]
            for sr in stmt_results:
                msg_lines.append(
                    f"- 第 {sr['index']} 条 `{sr['primary_verb']}`：影响 {sr['affected_rows']} 行，耗时 {sr['elapsed_ms']} ms"
                )
            if catalog_refreshed:
                msg_lines.append("- 已自动刷新目标连接库表元数据缓存。")
        else:
            last_fail = stmt_results[-1] if stmt_results else {"index": 1, "error": "未知错误"}
            msg_lines = [
                f"❌ **执行中断失败**（目标连接 `{profile.name}` · `{profile.db_type}` · 成功 {succeeded_cnt}/{len(assessment.statements)} 条）",
                f"- 失败位置：第 {last_fail['index']} 条 `{last_fail.get('primary_verb', '')}`",
                f"- 数据库报错：`{last_fail.get('error', '')}`",
            ]

        exec_msg = self._insert_message(
            session_id=prop["session_id"],
            turn_index=turn_idx,
            role="assistant",
            task_mode="ddl",
            response_type="execution",
            content="\n".join(msg_lines),
            proposal_id="",
        )
        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                "UPDATE ai_assistant_sessions SET updated_at = ? WHERE session_id = ?",
                (decided_at, prop["session_id"]),
            )

        updated_prop = self.get_proposal(proposal_id)
        return {
            "ok": True,
            "execution_ok": all_ok,
            "proposal": updated_prop,
            "execution_result": exec_summary,
            "execution_message": exec_msg,
            "catalog_refreshed": catalog_refreshed,
            "session": self.get_session_detail(prop["session_id"]),
        }

    def reject_proposal(self, proposal_id: str, *, reason: str = "") -> Dict[str, Any]:
        prop = self.get_proposal(proposal_id)
        if prop is None:
            raise KeyError("未找到指定的待确认执行提案")
        if prop["status"] != "pending":
            raise ValueError(f"该提案当前状态为 `{prop['status']}`，无需重复取消")

        now = _now_str()
        clean_reason = (reason or "").strip()[:240]
        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                """
                UPDATE ai_assistant_proposals
                SET status = 'rejected',
                    execution_result_json = ?,
                    decided_at = ?
                WHERE proposal_id = ?
                """,
                (
                    json.dumps({"rejected_reason": clean_reason, "rejected_at": now}, ensure_ascii=False),
                    now,
                    proposal_id,
                ),
            )
            conn.execute(
                "UPDATE ai_assistant_sessions SET updated_at = ? WHERE session_id = ?",
                (now, prop["session_id"]),
            )

        self.ctx.store.add_log(
            "INFO",
            "AI助手变更门禁",
            f"用户已取消提案 {proposal_id}" + (f"（原因: {clean_reason}）" if clean_reason else ""),
            sql_text=prop["original_sql"][:1000],
        )
        return {
            "ok": True,
            "proposal": self.get_proposal(proposal_id),
            "session": self.get_session_detail(prop["session_id"]),
            "message": "已取消执行该语句",
        }
