"""Data.O.G 持久化后台任务与 🎯 `/goal`「不达目的不结束」状态机引擎 (goal_runner).

架构特性：
1. 与 HTTP SSE 连接生命周期完全解耦：后台工作线程受 `cancel_ev`（显式 `/cancel` 接口）而非客户端断连控制；
2. 先落盘后广播：每个事件先写入 Layer 2 私有表 `ai_assistant_run_events` 获得单调 `event_id`，再推送给所有实时订阅者，支持 `Last-Event-ID` / `after_event_id` 零丢帧断点续流；
3. `/goal` 五态闭环：`probing -> planning -> running -> verifying -> completed | paused_recoverable`；
   - `verifying` 阶段执行覆盖率 + HIGH/MEDIUM 静态隐患逐项核验 + 临时表血缘连续性硬验收门禁；
   - 验收未通过自动生成 `remedial` 补救子任务回到 `running`，触及安全步数/壁钟护栏则优雅转入 `paused_recoverable` 保留完整 `checkpoint_json` 与 `ledger`，支持一键 `/resume` 接续执行绝不从头重跑。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from core.platform.context import PlatformContext
from core.platform.llm_hub import (
    ContextLengthExceededError,
    extract_json_payload,
    infer_model_capabilities,
)
from core.platform.storage_paths import redact_private_storage_paths
from modules.ai_assistant.attachments import (
    MAX_ATTACHMENTS_PER_TURN,
    compact_sql_for_llm,
    format_with_line_numbers,
)
from modules.ai_assistant.service import (
    AiAssistantService,
    ExecutionPlanner,
    infer_task_mode,
)
from modules.ai_assistant.sql_static_analyzer import (
    analyze_sql_static,
    extract_statement_source,
    format_static_analysis_brief,
)

TERMINAL_RUN_STATUSES = frozenset({"completed", "paused_recoverable", "cancelled", "failed"})
ACTIVE_RUN_STATUSES = frozenset({"queued", "probing", "planning", "running", "verifying"})

DEFAULT_MAX_GOAL_STEPS = 25
DEFAULT_GOAL_WALLCLOCK_SEC = 1800.0
MAX_REMEDIAL_ROUNDS = 3

_HUB_INIT_LOCK = threading.Lock()


def _now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _RunRuntimeHub:
    """绑定于单个 PlatformContext 的线程安全运行态注册表（支持多线程后台 Run 与多路 SSE 订阅者）."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel_events: Dict[str, threading.Event] = {}
        self._active_runs: Set[str] = set()
        self._subscribers: Dict[str, List[Tuple[asyncio.AbstractEventLoop, asyncio.Queue[Optional[Dict[str, Any]]]]]] = {}

    def register_run(self, run_id: str) -> threading.Event:
        with self._lock:
            ev = threading.Event()
            self._cancel_events[run_id] = ev
            self._active_runs.add(run_id)
            return ev

    def unregister_run(self, run_id: str) -> None:
        with self._lock:
            self._active_runs.discard(run_id)
            self._cancel_events.pop(run_id, None)

    def is_running(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active_runs

    def request_cancel(self, run_id: str) -> bool:
        with self._lock:
            ev = self._cancel_events.get(run_id)
            if ev is not None:
                ev.set()
                return True
            return False

    def subscribe(
        self,
        run_id: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Optional[Dict[str, Any]]],
    ) -> None:
        with self._lock:
            self._subscribers.setdefault(run_id, []).append((loop, queue))

    def unsubscribe(
        self,
        run_id: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Optional[Dict[str, Any]]],
    ) -> None:
        with self._lock:
            subs = self._subscribers.get(run_id)
            if not subs:
                return
            kept = [(lp, q) for lp, q in subs if q is not queue]
            if kept:
                self._subscribers[run_id] = kept
            else:
                self._subscribers.pop(run_id, None)

    def publish(self, run_id: str, event_rec: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers.get(run_id) or [])
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event_rec)
            except RuntimeError:
                pass

    def notify_stream_close(self, run_id: str) -> None:
        with self._lock:
            subs = list(self._subscribers.get(run_id) or [])
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, None)
            except RuntimeError:
                pass


def get_run_runtime_hub(ctx: PlatformContext) -> _RunRuntimeHub:
    hub = getattr(ctx, "_ai_assistant_run_hub", None)
    if isinstance(hub, _RunRuntimeHub):
        return hub
    with _HUB_INIT_LOCK:
        hub = getattr(ctx, "_ai_assistant_run_hub", None)
        if not isinstance(hub, _RunRuntimeHub):
            hub = _RunRuntimeHub()
            setattr(ctx, "_ai_assistant_run_hub", hub)
    return hub


def _sanitize_plan_for_storage(plan: Dict[str, Any]) -> Dict[str, Any]:
    """剥离大体量原文切片仅保留结构化计划与阶段 Checklist 落库."""
    out = dict(plan or {})
    out.pop("lineage_slice", None)
    return out


def _extract_addressed_findings_from_reply(
    raw_text: str,
    parsed_obj: Optional[Dict[str, Any]],
    candidate_findings: List[Dict[str, Any]],
) -> Tuple[Set[str], Dict[str, Dict[str, Any]]]:
    """从阶段输出中识别被明确核验判定的静态隐患 ID（兼容结构化 JSON 与 Markdown 回答）."""
    addressed: Set[str] = set()
    verdicts: Dict[str, Dict[str, Any]] = {}

    if isinstance(parsed_obj, dict):
        fv = parsed_obj.get("finding_verdicts")
        if isinstance(fv, dict):
            for k, v in fv.items():
                fid = str(k or "").strip().upper()
                if not fid:
                    continue
                if isinstance(v, dict):
                    status = str(v.get("status") or "confirmed").strip().lower()
                    reason = str(v.get("reason") or v.get("detail") or "").strip()
                    fix = str(v.get("fix") or "").strip()
                    if status or reason:
                        addressed.add(fid)
                        verdicts[fid] = {"status": status, "reason": reason[:240], "fix": fix[:240]}
                elif isinstance(v, str) and v.strip():
                    addressed.add(fid)
                    verdicts[fid] = {"status": "reviewed", "reason": v.strip()[:240], "fix": ""}
        for field_name, default_status in (("confirmed_issues", "confirmed"), ("false_positives", "false_positive")):
            items = parsed_obj.get(field_name)
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        fid = str(it.get("finding_id") or "").strip().upper()
                        if fid:
                            addressed.add(fid)
                            verdicts.setdefault(
                                fid,
                                {
                                    "status": default_status,
                                    "reason": str(it.get("detail") or it.get("title") or it.get("reason") or "")[:240],
                                    "fix": str(it.get("fix") or "")[:240],
                                },
                            )
                    else:
                        txt_it = str(it or "")
                        for m in re.finditer(r"\bF\d{3}\b", txt_it.upper()):
                            fid = m.group(0)
                            addressed.add(fid)
                            verdicts.setdefault(fid, {"status": default_status, "reason": txt_it[:240], "fix": ""})

    # 结合正文文本核对是否明确提及了 finding_id 或 (隐患类型 + 精确行号)
    search_corpus = (raw_text or "")
    if isinstance(parsed_obj, dict):
        search_corpus += "\n" + json.dumps(parsed_obj, ensure_ascii=False)
    up_corpus = search_corpus.upper()

    for f in candidate_findings:
        fid = str(f.get("finding_id") or "").upper()
        if not fid or fid in addressed:
            continue
        if re.search(rf"\b{re.escape(fid)}\b", up_corpus):
            addressed.add(fid)
            verdicts.setdefault(fid, {"status": "reviewed", "reason": f"已在阶段结论中核验 {fid}", "fix": ""})
            continue
        ln = int(f.get("line") or 0)
        code = str(f.get("code") or "").upper()
        if ln > 0 and (f"L{ln}" in up_corpus or f"第 {ln} 行" in search_corpus or f"第{ln}行" in search_corpus):
            if code and (
                code in up_corpus
                or (code == "UNBOUNDED_DML" and ("WHERE" in up_corpus or "全表" in search_corpus))
                or (code == "LOOP_DML" and ("循环" in search_corpus or "LOOP" in up_corpus))
                or (code == "DYNAMIC_SQL" and ("动态" in search_corpus or "EXECUTE" in up_corpus))
                or (code == "MID_COMMIT" and ("COMMIT" in up_corpus or "ROLLBACK" in up_corpus or "事务" in search_corpus))
                or (code == "DIALECT_COMPAT" and ("方言" in search_corpus or "兼容" in search_corpus or "ORACLE" in up_corpus))
            ):
                addressed.add(fid)
                verdicts.setdefault(fid, {"status": "reviewed", "reason": f"已结合 L{ln} 源码核验 {code}", "fix": ""})

    return addressed, verdicts


class GoalRunner:
    """持久化后台 Run 与 🎯 `/goal`「不达目的不结束」状态机调度器."""

    DEFAULT_WALLCLOCK_BUDGET_SEC = DEFAULT_GOAL_WALLCLOCK_SEC

    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx
        self.svc = AiAssistantService(ctx)
        self.hub = get_run_runtime_hub(ctx)

    def start_new_run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """校验参数、创建/绑定会话与用户消息、落盘 Run 并在后台线程异步推进."""
        req_aids: List[str] = []
        seen_aids: Set[str] = set()
        for raw_aid in (payload.get("attachment_ids") or []):
            s_aid = str(raw_aid or "").strip()
            if s_aid and s_aid not in seen_aids:
                seen_aids.add(s_aid)
                req_aids.append(s_aid)
            if len(req_aids) >= MAX_ATTACHMENTS_PER_TURN:
                break

        turn_attachments: List[Dict[str, Any]] = []
        for aid in req_aids:
            att = self.svc.get_attachment(aid, include_raw=False, include_chunks=False)
            if att is None:
                raise ValueError(f"找不到指定附件 `{aid}`（可能已被移除或过期）")
            turn_attachments.append(att)

        raw_prompt = str(payload.get("prompt") or "")
        clean_prompt = raw_prompt.strip()
        if not clean_prompt and not turn_attachments:
            raise ValueError("请输入要分析的目标问题，或先粘贴/上传 SQL/文本/CSV 文件")
        if not clean_prompt and turn_attachments:
            names_preview = "、".join(a["filename"] for a in turn_attachments[:3])
            clean_prompt = f"请对附带文件（{names_preview}）进行全链路深度分析、血缘梳理与隐患排查。"

        session_id = str(payload.get("session_id") or "").strip() or None
        sess = self.svc.get_session_detail(session_id) if session_id else None

        eff_provider_id = (str(payload.get("provider_id") or "") or (sess.get("provider_id") if sess else "") or "").strip()
        eff_model = (str(payload.get("model") or "") or (sess.get("model") if sess else "") or "").strip()

        resolved_llm = self.ctx.llm_hub.resolve_provider_and_model(
            provider_id=eff_provider_id, model=eff_model, require_ready=True
        )
        assert resolved_llm is not None
        llm_prof, eff_llm_model = resolved_llm

        req_tmode = str(payload.get("thinking_mode") or "auto").strip().lower()
        if req_tmode not in ("auto", "enabled", "disabled"):
            req_tmode = "auto"
        eff_thinking_mode = (
            req_tmode if req_tmode != "auto" or sess is None else (sess.get("thinking_mode") or "auto")
        )

        run_mode = str(payload.get("run_mode") or "goal").strip().lower()
        if run_mode not in ("standard", "goal"):
            run_mode = "goal"

        work_mode = str(payload.get("work_mode") or "auto").strip().lower()
        connection_id = str(payload.get("connection_id") or "").strip()
        dialect = str(payload.get("dialect") or "").strip()
        schema_name = str(payload.get("schema_name") or "").strip()
        focus_tables = payload.get("focus_tables") if isinstance(payload.get("focus_tables"), list) else None
        task_mode = str(payload.get("task_mode") or "auto").strip().lower()

        session_title_seed = (
            clean_prompt[:32]
            if raw_prompt.strip()
            else f"Goal分析 · {turn_attachments[0]['filename']}"[:32]
        )

        if sess is None:
            eff_mode, prof, eff_dialect, eff_schema = self.svc._resolve_scope(
                work_mode=work_mode,
                connection_id=connection_id,
                dialect=dialect,
                schema_name=schema_name,
            )
            sess = self.svc.create_session(
                title=session_title_seed,
                work_mode=eff_mode,
                connection_id=prof.connection_id if prof else "",
                dialect=eff_dialect,
                schema_name=eff_schema,
                focus_tables=focus_tables,
                task_mode=task_mode,
                provider_id=llm_prof.provider_id,
                model=eff_llm_model,
                thinking_mode=eff_thinking_mode,
            )
            sid = sess["session_id"]
        else:
            sid = sess["session_id"]
            eff_mode, prof, eff_dialect, eff_schema = self.svc._resolve_scope(
                work_mode=work_mode if work_mode != "auto" else sess["work_mode"],
                connection_id=connection_id if connection_id else (sess["connection_id"] if work_mode != "dialect" else ""),
                dialect=dialect or sess["dialect"],
                schema_name=schema_name or sess["schema_name"],
            )
            sess = (
                self.svc.update_session(
                    sid,
                    work_mode=eff_mode,
                    connection_id=prof.connection_id if prof else "",
                    dialect=eff_dialect,
                    schema_name=eff_schema,
                    focus_tables=focus_tables if focus_tables else sess["focus_tables"],
                    task_mode=task_mode if task_mode != "auto" else sess["task_mode"],
                    provider_id=llm_prof.provider_id,
                    model=eff_llm_model,
                    thinking_mode=eff_thinking_mode,
                )
                or sess
            )

        if req_aids:
            self.svc.bind_attachments_to_session(req_aids, sid)

        eff_task_mode = infer_task_mode(
            clean_prompt, task_mode if task_mode != "auto" else sess.get("task_mode", "auto")
        )
        attachments_meta = [self.svc._build_attachment_metadata(a) for a in turn_attachments]
        next_turn = int(sess.get("turn_count") or 0) + 1
        user_msg = self.svc._insert_message(
            session_id=sid,
            turn_index=next_turn,
            role="user",
            task_mode=eff_task_mode,
            response_type="prompt",
            content=clean_prompt,
            attachments=attachments_meta,
        )

        # 立即更新会话标题与轮次
        with self.ctx.store.module_db(scope="private") as conn:
            conn.execute(
                """
                UPDATE ai_assistant_sessions
                SET turn_count = ?, updated_at = ?, title = CASE WHEN turn_count = 0 THEN ? ELSE title END
                WHERE session_id = ?
                """,
                (next_turn, _now_str(), session_title_seed[:36], sid),
            )

        max_goal_steps = max(1, min(40, int(payload.get("max_goal_steps") or DEFAULT_MAX_GOAL_STEPS)))
        safe_request = {
            "session_id": sid,
            "prompt": clean_prompt,
            "work_mode": eff_mode,
            "connection_id": prof.connection_id if prof else "",
            "dialect": eff_dialect,
            "schema_name": eff_schema,
            "focus_tables": list(sess.get("focus_tables") or []),
            "task_mode": eff_task_mode,
            "provider_id": llm_prof.provider_id,
            "model": eff_llm_model,
            "thinking_mode": eff_thinking_mode,
            "max_steps": max(1, min(10, int(payload.get("max_steps") or 6))),
            "max_goal_steps": max_goal_steps,
            "sample_limit": max(10, min(200, int(payload.get("sample_limit") or 100))),
            "attachment_ids": req_aids,
            "large_payload_strategy": str(payload.get("large_payload_strategy") or "auto"),
            "batch_start_chunk": int(payload.get("batch_start_chunk") or 0),
            "batch_max_chunks": int(payload.get("batch_max_chunks") or 8),
            "turn_index": next_turn,
            "run_mode": run_mode,
        }

        run_rec = self.svc.create_run(
            session_id=sid,
            user_message_id=int(user_msg["id"]),
            run_mode=run_mode,
            request_payload=safe_request,
            provider_id=llm_prof.provider_id,
            model=eff_llm_model,
            thinking_mode=eff_thinking_mode,
        )
        run_id = run_rec["run_id"]

        # 持久化初始 session_init 事件
        ev_init = self.svc.append_run_event(
            run_id,
            "session_init",
            {
                "type": "session_init",
                "run_id": run_id,
                "run_mode": run_mode,
                "session": {k: v for k, v in sess.items() if k != "messages"},
                "user_message": user_msg,
                "thinking_mode": eff_thinking_mode,
            },
        )
        self.hub.publish(run_id, ev_init)

        cancel_ev = self.hub.register_run(run_id)
        thr = threading.Thread(
            target=self._run_worker,
            args=(run_id, cancel_ev, False),
            name=f"ai_goal_{run_id[:10]}",
            daemon=True,
        )
        thr.start()

        refreshed_run = self.svc.get_run(run_id) or run_rec
        return {
            "ok": True,
            "run": refreshed_run,
            "session": self.svc.get_session_detail(sid),
            "user_message": user_msg,
        }

    def resume_run(self, run_id: str, *, extra_steps: int = 15) -> Dict[str, Any]:
        """从 SQLite Checkpoint 断点继续推进 `paused_recoverable` / `failed` / `cancelled` 任务，绝不从头重跑."""
        run_rec = self.svc.get_run(run_id)
        if run_rec is None:
            raise KeyError("未找到指定的后台任务")
        if self.hub.is_running(run_id):
            return {"ok": True, "run": run_rec, "message": "任务正在后台执行中"}

        status = str(run_rec.get("status") or "")
        if status == "completed":
            raise ValueError("该任务已完成，无需继续执行")
        if status not in ("paused_recoverable", "failed", "cancelled", "queued"):
            raise ValueError(f"任务当前状态为 `{status}`，无法续跑")

        req = dict(run_rec.get("request") or {})
        ckpt = dict(run_rec.get("checkpoint") or {})
        steps_done = int(ckpt.get("steps_executed") or 0)
        cur_cap = int(req.get("max_goal_steps") or DEFAULT_MAX_GOAL_STEPS)
        req["max_goal_steps"] = min(60, max(cur_cap, steps_done + max(1, int(extra_steps or 15))))

        updated = self.svc.update_run(
            run_id,
            status="running",
            request_payload=req,
            current_stage="从断点恢复执行",
            error_message="",
            finished_at="",
        )
        ev_res = self.svc.append_run_event(
            run_id,
            "status",
            {
                "type": "status",
                "run_id": run_id,
                "stage": "从断点恢复执行",
                "detail": f"已加载 Checkpoint（已完成 {len(ckpt.get('completed_stage_ids') or [])} 个阶段），正在接续剩余目标...",
                "percent": int((updated or run_rec).get("progress_pct") or 0),
                "resumed": True,
            },
        )
        self.hub.publish(run_id, ev_res)

        cancel_ev = self.hub.register_run(run_id)
        thr = threading.Thread(
            target=self._run_worker,
            args=(run_id, cancel_ev, True),
            name=f"ai_goal_resume_{run_id[:10]}",
            daemon=True,
        )
        thr.start()
        return {"ok": True, "run": self.svc.get_run(run_id) or updated}

    def cancel_run(self, run_id: str) -> Dict[str, Any]:
        """显式停止后台运行中的任务并保留截至当前已完成的 Checkpoint."""
        run_rec = self.svc.get_run(run_id)
        if run_rec is None:
            raise KeyError("未找到指定的后台任务")
        was_active = self.hub.request_cancel(run_id)
        if not was_active and run_rec.get("status") not in ("completed", "cancelled"):
            updated = self.svc.update_run(
                run_id,
                status="cancelled",
                current_stage="已停止",
                finished_at=_now_str(),
            )
            ev = self.svc.append_run_event(
                run_id,
                "run_cancelled",
                {"type": "run_cancelled", "run_id": run_id, "status": "cancelled", "run": updated},
            )
            self.hub.publish(run_id, ev)
            self.hub.notify_stream_close(run_id)
            return {"ok": True, "run": updated}
        return {"ok": True, "run": self.svc.get_run(run_id) or run_rec}

    def _emit(self, run_id: str, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        ev_rec = self.svc.append_run_event(run_id, event_type, payload)
        self.hub.publish(run_id, ev_rec)
        return ev_rec

    def _pause_recoverable(
        self,
        run_id: str,
        *,
        plan: Dict[str, Any],
        checkpoint: Dict[str, Any],
        progress_pct: int,
        reason: str,
    ) -> None:
        updated = self.svc.update_run(
            run_id,
            status="paused_recoverable",
            plan=_sanitize_plan_for_storage(plan),
            checkpoint=checkpoint,
            progress_pct=progress_pct,
            current_stage="已保存断点（可一键继续）",
            error_message=reason[:500],
        )
        self._emit(
            run_id,
            "run_paused",
            {
                "type": "run_paused",
                "run_id": run_id,
                "status": "paused_recoverable",
                "progress_pct": progress_pct,
                "reason": reason,
                "plan": _sanitize_plan_for_storage(plan),
                "checkpoint": checkpoint,
                "run": updated,
            },
        )

    def _finalize_cancelled(
        self,
        run_id: str,
        *,
        plan: Dict[str, Any],
        checkpoint: Dict[str, Any],
        progress_pct: int,
    ) -> None:
        updated = self.svc.update_run(
            run_id,
            status="cancelled",
            plan=_sanitize_plan_for_storage(plan),
            checkpoint=checkpoint,
            progress_pct=progress_pct,
            current_stage="已手动停止",
            finished_at=_now_str(),
        )
        self._emit(
            run_id,
            "run_cancelled",
            {
                "type": "run_cancelled",
                "run_id": run_id,
                "status": "cancelled",
                "progress_pct": progress_pct,
                "plan": _sanitize_plan_for_storage(plan),
                "checkpoint": checkpoint,
                "run": updated,
            },
        )

    def _run_worker(self, run_id: str, cancel_ev: threading.Event, is_resume: bool) -> None:
        t_start = time.perf_counter()
        plan: Dict[str, Any] = {}
        checkpoint: Dict[str, Any] = {}
        progress_pct = 0

        try:
            run_rec = self.svc.get_run(run_id)
            if run_rec is None:
                return
            req = dict(run_rec.get("request") or {})
            sid = str(run_rec.get("session_id") or "")
            run_mode = str(run_rec.get("run_mode") or "goal")
            provider_id = str(run_rec.get("provider_id") or req.get("provider_id") or "")
            model = str(run_rec.get("model") or req.get("model") or "")
            thinking_mode = str(run_rec.get("thinking_mode") or req.get("thinking_mode") or "auto")
            dialect = str(req.get("dialect") or "DWS")
            clean_prompt = str(req.get("prompt") or "")
            max_goal_steps = max(1, int(req.get("max_goal_steps") or DEFAULT_MAX_GOAL_STEPS))
            att_ids = list(req.get("attachment_ids") or [])

            plan = dict(run_rec.get("plan") or {})
            checkpoint = dict(run_rec.get("checkpoint") or {})
            progress_pct = int(run_rec.get("progress_pct") or 0)

            turn_attachments: List[Dict[str, Any]] = []
            for aid in att_ids:
                att = self.svc.get_attachment(aid, include_raw=True, include_chunks=True)
                if att is not None:
                    if att.get("file_kind") == "sql" and not (att.get("analysis") or {}).get("statements"):
                        att["analysis"] = analyze_sql_static(str(att.get("raw_text") or ""), dialect=dialect)
                    turn_attachments.append(att)

            # =================================================================
            # Phase A: probing (动态能力探查，命中 24h 缓存零延迟)
            # =================================================================
            model_caps: Dict[str, Any] = dict(checkpoint.get("model_caps") or {})
            if not is_resume or not model_caps:
                if cancel_ev.is_set():
                    self._finalize_cancelled(run_id, plan=plan, checkpoint=checkpoint, progress_pct=progress_pct)
                    return
                progress_pct = 5
                self.svc.update_run(
                    run_id, status="probing", progress_pct=progress_pct, current_stage="探查模型能力与窗口"
                )
                self._emit(
                    run_id,
                    "status",
                    {
                        "type": "status",
                        "stage": "探查模型能力",
                        "detail": f"正在核验 {model} 真实上下文窗口、有效推理甜蜜区与延迟基准...",
                        "percent": progress_pct,
                    },
                )
                try:
                    probe_res = self.ctx.llm_hub.probe_model_capabilities(
                        provider_id=provider_id,
                        model=model,
                        force_refresh=False,
                        allow_fallback=True,
                    )
                    model_caps = dict(probe_res.get("capabilities") or {})
                    self._emit(
                        run_id,
                        "probe_ready",
                        {
                            "type": "probe_ready",
                            "model": model,
                            "provider_id": provider_id,
                            "cached": bool(probe_res.get("cached")),
                            "probe_ok": bool(probe_res.get("probe_ok", True)),
                            "capabilities": model_caps,
                        },
                    )
                except Exception:
                    resolved = self.ctx.llm_hub.resolve_provider_and_model(
                        provider_id=provider_id, model=model, require_ready=False
                    )
                    if resolved is not None:
                        p_obj, m_id = resolved
                        model_caps = infer_model_capabilities(m_id, p_obj.provider_type, p_obj.base_url, p_obj.extra)
                    else:
                        model_caps = infer_model_capabilities(model)
                checkpoint["model_caps"] = model_caps

            # =================================================================
            # Phase B: planning (制定执行策略与阶段 Checklist)
            # =================================================================
            if not is_resume or not plan.get("stages"):
                if cancel_ev.is_set():
                    self._finalize_cancelled(run_id, plan=plan, checkpoint=checkpoint, progress_pct=progress_pct)
                    return
                progress_pct = 10
                self.svc.update_run(
                    run_id, status="planning", progress_pct=progress_pct, current_stage="制定最优拆解计划"
                )
                self._emit(
                    run_id,
                    "status",
                    {
                        "type": "status",
                        "stage": "制定拆解计划",
                        "detail": "正在综合 SQL 静态血缘图纸与模型甜蜜区容量编排阶段清单...",
                        "percent": progress_pct,
                    },
                )
                planned = ExecutionPlanner.plan(
                    model_caps=model_caps,
                    turn_attachments=turn_attachments,
                    clean_prompt=clean_prompt,
                    req_strategy=str(req.get("large_payload_strategy") or "auto"),
                    run_mode=run_mode,
                    dialect=dialect,
                    batch_start_chunk=int(req.get("batch_start_chunk") or 0),
                )
                plan = _sanitize_plan_for_storage(planned)
                checkpoint.setdefault("stage_cursor", 0)
                checkpoint.setdefault("completed_stage_ids", [])
                checkpoint.setdefault("covered_stmt_ids", [])
                checkpoint.setdefault("covered_line_ranges", [])
                checkpoint.setdefault("addressed_finding_ids", [])
                checkpoint.setdefault("finding_verdicts", {})
                checkpoint.setdefault("remedial_rounds", 0)
                checkpoint.setdefault("steps_executed", 0)
                checkpoint.setdefault("retry_count", 0)
                checkpoint.setdefault("reasoning_segments", [])
                checkpoint.setdefault("reasoning_ms", 0.0)
                checkpoint.setdefault("steps_trace", [])
                checkpoint.setdefault(
                    "ledger",
                    {
                        "stage_summaries": [],
                        "confirmed_issues": [],
                        "false_positives": [],
                        "lineage_notes": [],
                        "open_questions": [],
                    },
                )
                self.svc.update_run(
                    run_id,
                    status="planning",
                    plan=plan,
                    checkpoint=checkpoint,
                    progress_pct=progress_pct,
                    current_stage="执行计划已就绪",
                )
                self._emit(
                    run_id,
                    "plan_ready",
                    {
                        "type": "plan_ready",
                        "plan": plan,
                        "checkpoint": checkpoint,
                    },
                )

            # 收集附件静态分析上下文索引
            att_by_id: Dict[str, Dict[str, Any]] = {
                str(a.get("attachment_id") or ""): a for a in turn_attachments
            }
            all_static_findings: List[Dict[str, Any]] = []
            finding_by_id: Dict[str, Dict[str, Any]] = {}
            all_stmt_ids_expected: List[str] = []
            lineage_brief_blocks: List[str] = []

            for att in turn_attachments:
                an = att.get("analysis") if isinstance(att.get("analysis"), dict) else {}
                if an:
                    for f in an.get("static_findings") or []:
                        f_copy = dict(f)
                        f_copy["attachment_id"] = att["attachment_id"]
                        f_copy["filename"] = att["filename"]
                        all_static_findings.append(f_copy)
                        finding_by_id[str(f["finding_id"]).upper()] = f_copy
                    brief = format_static_analysis_brief(
                        an, max_lineage_lines=10, max_findings=16, max_statements=16
                    )
                    if brief:
                        lineage_brief_blocks.append(f"### 附件 `{att['filename']}` 静态图纸\n{brief}")

            # 期望覆盖的语句集合（血缘切片策略以计划内语句为准，全量/分段策略覆盖计划全部语句）
            for st in plan.get("stages") or []:
                for s_id in st.get("stmt_ids") or []:
                    if s_id not in all_stmt_ids_expected:
                        all_stmt_ids_expected.append(s_id)

            # 期望核验的 HIGH/MEDIUM 静态隐患集合
            if plan.get("strategy") == "lineage_sliced":
                exp_sids_set = set(all_stmt_ids_expected)
                required_hm_findings = [
                    f
                    for f in all_static_findings
                    if f.get("severity") in ("HIGH", "MEDIUM")
                    and (not f.get("stmt_id") or f["stmt_id"] in exp_sids_set)
                ]
            else:
                required_hm_findings = [
                    f for f in all_static_findings if f.get("severity") in ("HIGH", "MEDIUM")
                ]

            used_provider_name = ""
            used_model = model

            # =================================================================
            # Phase C & D: running <-> verifying 硬验收与自动补救闭环
            # =================================================================
            while True:
                if cancel_ev.is_set():
                    self._finalize_cancelled(run_id, plan=plan, checkpoint=checkpoint, progress_pct=progress_pct)
                    return

                stages: List[Dict[str, Any]] = list(plan.get("stages") or [])
                completed_set: Set[str] = set(checkpoint.get("completed_stage_ids") or [])
                pending_stages = [s for s in stages if s.get("stage_id") not in completed_set]

                self.svc.update_run(
                    run_id,
                    status="running",
                    plan=plan,
                    checkpoint=checkpoint,
                    current_stage="分阶段深度推进中",
                )

                for st in pending_stages:
                    if cancel_ev.is_set():
                        self._finalize_cancelled(run_id, plan=plan, checkpoint=checkpoint, progress_pct=progress_pct)
                        return

                    steps_executed = int(checkpoint.get("steps_executed") or 0)
                    elapsed_wall = time.perf_counter() - t_start
                    if steps_executed >= max_goal_steps:
                        self._pause_recoverable(
                            run_id,
                            plan=plan,
                            checkpoint=checkpoint,
                            progress_pct=progress_pct,
                            reason=f"已达到单轮安全步数护栏 ({steps_executed}/{max_goal_steps} 步)，已完整保存阶段账本与 Checkpoint，点击「从断点继续执行」即可接力推进剩余目标。",
                        )
                        return
                    if elapsed_wall >= float(self.DEFAULT_WALLCLOCK_BUDGET_SEC):
                        self._pause_recoverable(
                            run_id,
                            plan=plan,
                            checkpoint=checkpoint,
                            progress_pct=progress_pct,
                            reason="已达到单次连续执行时长护栏，已完整保存 Checkpoint，点击「从断点继续执行」可无缝继续。",
                        )
                        return

                    st["status"] = "running"
                    total_s = max(1, len(stages))
                    done_before = len(completed_set)
                    progress_pct = min(84, 14 + int((done_before / total_s) * 68))
                    s_ln = int(st.get("start_line") or 0)
                    e_ln = int(st.get("end_line") or 0)
                    range_lbl = f"L{s_ln}-L{e_ln}" if s_ln > 0 else "全局探索"

                    self.svc.update_run(
                        run_id,
                        status="running",
                        plan=plan,
                        checkpoint=checkpoint,
                        progress_pct=progress_pct,
                        current_stage=st.get("title") or st.get("stage_id", ""),
                    )
                    self._emit(
                        run_id,
                        "stage_start",
                        {
                            "type": "stage_start",
                            "stage_id": st["stage_id"],
                            "index": int(st.get("index") or (done_before + 1)),
                            "total": total_s,
                            "title": st.get("title") or st["stage_id"],
                            "range_label": range_lbl,
                            "kind": st.get("kind") or "analysis",
                            "progress_pct": progress_pct,
                        },
                    )

                    # 构造本阶段高信噪比上下文（静态图纸 + 只读账本 + 精准源码片段）
                    stage_msgs = self._build_stage_messages(
                        stage=st,
                        clean_prompt=clean_prompt,
                        dialect=dialect,
                        att_by_id=att_by_id,
                        finding_by_id=finding_by_id,
                        checkpoint=checkpoint,
                        lineage_brief="\n\n".join(lineage_brief_blocks)[:3000],
                        force_compact=bool(st.get("use_compact_view")),
                    )

                    t_stage = time.perf_counter()
                    stage_reply = ""
                    stage_reasoning = ""
                    stage_think_ms = 0.0
                    stage_cancelled = False

                    for ctx_retry in range(2):
                        try:
                            (
                                stage_reply,
                                stage_reasoning,
                                stage_think_ms,
                                s_prov,
                                s_mdl,
                                stage_cancelled,
                            ) = self._invoke_stage_stream(
                                run_id=run_id,
                                messages=stage_msgs,
                                provider_id=provider_id,
                                model=model,
                                thinking_mode=thinking_mode,
                                step_no=steps_executed + 1,
                                cancel_ev=cancel_ev,
                                checkpoint=checkpoint,
                            )
                            used_provider_name = s_prov or used_provider_name
                            used_model = s_mdl or used_model
                            break
                        except ContextLengthExceededError as ctx_exc:
                            if ctx_retry == 0:
                                st["use_compact_view"] = True
                                self._emit(
                                    run_id,
                                    "status",
                                    {
                                        "type": "status",
                                        "stage": "自动压缩减载",
                                        "detail": f"阶段 {st['stage_id']} 触发上游窗口限制，已自动校准窗口并切换语义压缩视图重试...",
                                        "percent": progress_pct,
                                    },
                                )
                                stage_msgs = self._build_stage_messages(
                                    stage=st,
                                    clean_prompt=clean_prompt,
                                    dialect=dialect,
                                    att_by_id=att_by_id,
                                    finding_by_id=finding_by_id,
                                    checkpoint=checkpoint,
                                    lineage_brief="\n\n".join(lineage_brief_blocks)[:1600],
                                    force_compact=True,
                                    max_code_tokens=max(2500, int(model_caps.get("recommended_chunk_tokens", 5500) * 0.6)),
                                )
                                continue
                            raise ctx_exc
                        except Exception as stage_exc:
                            safe_err = redact_private_storage_paths(str(stage_exc), ctx=self.ctx)
                            st["status"] = "pending"
                            self._pause_recoverable(
                                run_id,
                                plan=plan,
                                checkpoint=checkpoint,
                                progress_pct=progress_pct,
                                reason=f"阶段 [{st.get('title')}] 在多次重试后仍遇到上游异常：{safe_err}。已完成 {len(completed_set)} 个阶段均已保存，点击「从断点继续执行」即可继续。",
                            )
                            return

                    if stage_cancelled or cancel_ev.is_set():
                        st["status"] = "pending"
                        self._finalize_cancelled(run_id, plan=plan, checkpoint=checkpoint, progress_pct=progress_pct)
                        return

                    stage_ms = round((time.perf_counter() - t_stage) * 1000.0, 1)
                    if stage_reasoning.strip():
                        checkpoint["reasoning_segments"].append(stage_reasoning.strip())
                    checkpoint["reasoning_ms"] = round(
                        float(checkpoint.get("reasoning_ms") or 0.0) + float(stage_think_ms or 0.0), 1
                    )

                    # 解析并合并阶段账本
                    parsed_stage: Optional[Dict[str, Any]] = None
                    try:
                        obj = extract_json_payload(stage_reply)
                        if isinstance(obj, dict):
                            parsed_stage = obj
                    except Exception:
                        parsed_stage = None

                    stage_f_objs = [
                        finding_by_id[fid.upper()]
                        for fid in (st.get("finding_ids") or [])
                        if fid.upper() in finding_by_id
                    ]
                    if not stage_f_objs and st.get("kind") == "remedial":
                        stage_f_objs = required_hm_findings
                    # 同时扫描全量 required_hm_findings 以防模型顺带核验了相邻隐患
                    scan_candidates = list({f["finding_id"]: f for f in (stage_f_objs + required_hm_findings)}.values())
                    newly_addressed, new_verdicts = _extract_addressed_findings_from_reply(
                        stage_reply, parsed_stage, scan_candidates
                    )

                    ledger = checkpoint["ledger"]
                    if isinstance(parsed_stage, dict):
                        st_summary = str(parsed_stage.get("summary") or "").strip() or stage_reply[:200]
                        for item in parsed_stage.get("confirmed_issues") or []:
                            val = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item).strip()
                            if val and val not in ledger["confirmed_issues"]:
                                ledger["confirmed_issues"].append(val[:400])
                        for item in parsed_stage.get("false_positives") or []:
                            val = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item).strip()
                            if val and val not in ledger["false_positives"]:
                                ledger["false_positives"].append(val[:300])
                        for item in parsed_stage.get("lineage_notes") or []:
                            val = str(item or "").strip()
                            if val and val not in ledger["lineage_notes"]:
                                ledger["lineage_notes"].append(val[:300])
                        # 若为补救阶段且解决了问题，清空已解决的 open_questions
                        if st.get("kind") == "remedial":
                            ledger["open_questions"] = [
                                str(x).strip()[:240]
                                for x in (parsed_stage.get("open_questions") or [])
                                if str(x).strip()
                            ][:5]
                        else:
                            for q in parsed_stage.get("open_questions") or []:
                                qs = str(q or "").strip()
                                if qs and qs not in ledger["open_questions"]:
                                    ledger["open_questions"].append(qs[:240])
                    else:
                        st_summary = re.sub(r"\s+", " ", stage_reply).strip()[:220] or "已完成本阶段源码核查"
                        ledger["confirmed_issues"].append(f"[{st['stage_id']} {range_lbl}] {stage_reply[:400]}")

                    ledger["stage_summaries"].append(
                        {
                            "stage_id": st["stage_id"],
                            "title": st.get("title") or st["stage_id"],
                            "range_label": range_lbl,
                            "kind": st.get("kind") or "analysis",
                            "summary": st_summary[:280],
                        }
                    )

                    # 更新覆盖率与隐患核验集合
                    cov_stmts = set(checkpoint.get("covered_stmt_ids") or [])
                    for sid_st in st.get("stmt_ids") or []:
                        cov_stmts.add(str(sid_st).upper())
                    if isinstance(parsed_stage, dict):
                        for sid_st in parsed_stage.get("covered_stmts") or []:
                            if str(sid_st).strip():
                                cov_stmts.add(str(sid_st).strip().upper())
                    checkpoint["covered_stmt_ids"] = sorted(cov_stmts)

                    if s_ln > 0 and e_ln >= s_ln:
                        rng_rec = f"{st.get('filename', '')}:L{s_ln}-L{e_ln}"
                        if rng_rec not in checkpoint["covered_line_ranges"]:
                            checkpoint["covered_line_ranges"].append(rng_rec)

                    addr_set = set(checkpoint.get("addressed_finding_ids") or []) | newly_addressed
                    checkpoint["addressed_finding_ids"] = sorted(addr_set)
                    checkpoint["finding_verdicts"].update(new_verdicts)

                    st["status"] = "done"
                    completed_set.add(st["stage_id"])
                    checkpoint["completed_stage_ids"] = list(completed_set)
                    checkpoint["stage_cursor"] = len(completed_set)
                    checkpoint["steps_executed"] = steps_executed + 1

                    step_rec = {
                        "step": len(checkpoint["steps_trace"]) + 1,
                        "tool": "goal_remedial_stage" if st.get("kind") == "remedial" else "goal_stage_analyze",
                        "purpose": st.get("title") or st["stage_id"],
                        "args": {"stage_id": st["stage_id"], "range": range_lbl},
                        "ok": True,
                        "elapsed_ms": stage_ms,
                        "summary": st_summary[:180],
                    }
                    checkpoint["steps_trace"].append(step_rec)

                    progress_pct = min(85, 14 + int((len(completed_set) / max(1, len(stages))) * 70))
                    self.svc.update_run(
                        run_id,
                        status="running",
                        plan=plan,
                        checkpoint=checkpoint,
                        progress_pct=progress_pct,
                        current_stage=f"已完成 {st.get('title') or st['stage_id']}",
                    )
                    self._emit(
                        run_id,
                        "stage_done",
                        {
                            "type": "stage_done",
                            "stage_id": st["stage_id"],
                            "index": int(st.get("index") or len(completed_set)),
                            "total": len(stages),
                            "title": st.get("title") or st["stage_id"],
                            "kind": st.get("kind") or "analysis",
                            "ok": True,
                            "elapsed_ms": stage_ms,
                            "summary": st_summary[:180],
                            "progress_pct": progress_pct,
                        },
                    )
                    self._emit(
                        run_id,
                        "checkpoint_saved",
                        {
                            "type": "checkpoint_saved",
                            "stage_cursor": checkpoint["stage_cursor"],
                            "completed_stages": len(completed_set),
                            "total_stages": len(stages),
                            "covered_statements": len(checkpoint["covered_stmt_ids"]),
                            "addressed_findings": len(checkpoint["addressed_finding_ids"]),
                            "progress_pct": progress_pct,
                        },
                    )

                # =============================================================
                # Phase D: verifying (四道硬验收门禁)
                # =============================================================
                progress_pct = 87
                self.svc.update_run(
                    run_id,
                    status="verifying",
                    plan=plan,
                    checkpoint=checkpoint,
                    progress_pct=progress_pct,
                    current_stage="执行覆盖率与隐患硬验收",
                )

                covered_stmts_set = {s.upper() for s in (checkpoint.get("covered_stmt_ids") or [])}
                missing_stmt_ids = [s for s in all_stmt_ids_expected if s.upper() not in covered_stmts_set]
                if all_stmt_ids_expected:
                    cov_pct = int(round(((len(all_stmt_ids_expected) - len(missing_stmt_ids)) / len(all_stmt_ids_expected)) * 100))
                else:
                    cov_pct = int(round((len(completed_set) / max(1, len(stages))) * 100))

                addr_fids_set = {f.upper() for f in (checkpoint.get("addressed_finding_ids") or [])}
                missed_findings = [
                    f for f in required_hm_findings if str(f.get("finding_id") or "").upper() not in addr_fids_set
                ]

                # 门禁 3：检查 temp_staging 上游写语句是否断链
                broken_lineage_sids: List[str] = []
                for att in turn_attachments:
                    an = att.get("analysis") if isinstance(att.get("analysis"), dict) else {}
                    for nd in (an.get("lineage_dag") or {}).get("nodes") or []:
                        if nd.get("role") == "temp_staging":
                            for wsid in nd.get("written_by") or []:
                                if wsid in all_stmt_ids_expected and wsid.upper() not in covered_stmts_set:
                                    if wsid not in broken_lineage_sids:
                                        broken_lineage_sids.append(wsid)

                remedial_rounds = int(checkpoint.get("remedial_rounds") or 0)
                gate_passed = (
                    len(missing_stmt_ids) == 0
                    and len(missed_findings) == 0
                    and len(broken_lineage_sids) == 0
                )

                verify_reasons: List[str] = []
                if missing_stmt_ids:
                    verify_reasons.append(f"遗漏语句 {len(missing_stmt_ids)} 条 ({', '.join(missing_stmt_ids[:5])})")
                if missed_findings:
                    mf_txt = "、".join(
                        f"{f['finding_id']}[{f['severity']}] L{f['line']}" for f in missed_findings[:5]
                    )
                    verify_reasons.append(f"尚未核验判定 {len(missed_findings)} 项 HIGH/MEDIUM 静态隐患 ({mf_txt})")
                if broken_lineage_sids:
                    verify_reasons.append(f"临时表血缘前置语句未覆盖 ({', '.join(broken_lineage_sids[:4])})")

                verify_detail = (
                    f"硬验收通过：覆盖率 {cov_pct}%，全部 {len(required_hm_findings)} 项 HIGH/MEDIUM 静态隐患均已明确判定。"
                    if gate_passed
                    else "硬验收未通过（"
                    + "；".join(verify_reasons)
                    + (
                        f"），正在自动生成第 {remedial_rounds + 1} 轮补救子任务继续核查！"
                        if remedial_rounds < MAX_REMEDIAL_ROUNDS
                        else "），已达最大补救轮次，将在终稿中显式标出待人工复核项。"
                    )
                )

                self._emit(
                    run_id,
                    "verify_result",
                    {
                        "type": "verify_result",
                        "passed": gate_passed,
                        "coverage_pct": cov_pct,
                        "covered_statements": len(all_stmt_ids_expected) - len(missing_stmt_ids),
                        "total_statements": len(all_stmt_ids_expected),
                        "verified_findings": len(required_hm_findings) - len(missed_findings),
                        "total_hm_findings": len(required_hm_findings),
                        "missed_findings": [
                            {
                                "finding_id": f["finding_id"],
                                "code": f["code"],
                                "severity": f["severity"],
                                "line": f["line"],
                                "title": f["title"],
                            }
                            for f in missed_findings[:10]
                        ],
                        "missing_stmt_ids": missing_stmt_ids[:15],
                        "remedial_round": remedial_rounds,
                        "detail": verify_detail,
                    },
                )

                if gate_passed or remedial_rounds >= MAX_REMEDIAL_ROUNDS:
                    checkpoint["verification_summary"] = {
                        "passed": gate_passed,
                        "coverage_pct": cov_pct,
                        "missed_finding_ids": [f["finding_id"] for f in missed_findings],
                        "missing_stmt_ids": missing_stmt_ids,
                        "detail": verify_detail,
                    }
                    break

                # 生成定向补救子任务并回到 running
                remedial_rounds += 1
                checkpoint["remedial_rounds"] = remedial_rounds
                rem_stage = self._build_remedial_stage(
                    remedial_round=remedial_rounds,
                    next_index=len(stages) + 1,
                    missed_findings=missed_findings,
                    missing_stmt_ids=sorted(set(missing_stmt_ids + broken_lineage_sids)),
                    att_by_id=att_by_id,
                    use_compact=bool(plan.get("use_compact_view")),
                )
                stages.append(rem_stage)
                plan["stages"] = stages
                self.svc.update_run(
                    run_id,
                    status="running",
                    plan=plan,
                    checkpoint=checkpoint,
                    current_stage=rem_stage["title"],
                )
                self._emit(
                    run_id,
                    "plan_ready",
                    {
                        "type": "plan_ready",
                        "plan": plan,
                        "checkpoint": checkpoint,
                        "remedial_added": True,
                    },
                )

                # 若已触及步数上限，优雅暂停等待用户一键续跑完成补救阶段
                if int(checkpoint.get("steps_executed") or 0) >= max_goal_steps:
                    self._pause_recoverable(
                        run_id,
                        plan=plan,
                        checkpoint=checkpoint,
                        progress_pct=progress_pct,
                        reason=f"验收发现遗漏项并已生成补救任务「{rem_stage['title']}」，但已达当前步数上限，点击「从断点继续执行」将直接执行补救并完成报告。",
                    )
                    return

            # =================================================================
            # Phase E: completed (全景综合报告生成 + length 自动续写)
            # =================================================================
            if cancel_ev.is_set():
                self._finalize_cancelled(run_id, plan=plan, checkpoint=checkpoint, progress_pct=progress_pct)
                return

            progress_pct = 92
            self.svc.update_run(
                run_id,
                status="running",
                plan=plan,
                checkpoint=checkpoint,
                progress_pct=progress_pct,
                current_stage="合成全景结构化最终报告",
            )
            self._emit(
                run_id,
                "status",
                {
                    "type": "status",
                    "stage": "合成全景报告",
                    "detail": "验收通过，正在基于全阶段账本与血缘图谱合成最终分析报告...",
                    "percent": progress_pct,
                },
            )

            synth_msgs = self._build_synthesis_messages(
                clean_prompt=clean_prompt,
                dialect=dialect,
                plan=plan,
                checkpoint=checkpoint,
                lineage_brief="\n\n".join(lineage_brief_blocks)[:3500],
                required_hm_findings=required_hm_findings,
            )

            final_md, synth_reasoning, synth_think_ms, syn_prov, syn_mdl, syn_cancelled = (
                self._invoke_stage_stream(
                    run_id=run_id,
                    messages=synth_msgs,
                    provider_id=provider_id,
                    model=model,
                    thinking_mode=thinking_mode,
                    step_no=int(checkpoint.get("steps_executed") or 0) + 1,
                    cancel_ev=cancel_ev,
                    checkpoint=checkpoint,
                    emit_content_deltas=True,
                )
            )
            used_provider_name = syn_prov or used_provider_name
            used_model = syn_mdl or used_model
            if synth_reasoning.strip():
                checkpoint["reasoning_segments"].append(synth_reasoning.strip())
            checkpoint["reasoning_ms"] = round(
                float(checkpoint.get("reasoning_ms") or 0.0) + float(synth_think_ms or 0.0), 1
            )

            if syn_cancelled or cancel_ev.is_set():
                checkpoint["partial_markdown"] = final_md
                self._finalize_cancelled(run_id, plan=plan, checkpoint=checkpoint, progress_pct=progress_pct)
                return

            _, meta_obj, clean_final_md, _ = AiAssistantService._parse_agent_response(final_md)
            content_md = (clean_final_md or final_md or "已完成全阶段目标分析。").strip()
            next_steps_list = AiAssistantService._normalize_next_steps((meta_obj or {}).get("next_steps"))

            total_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
            combined_reasoning = "\n\n---\n\n".join(checkpoint.get("reasoning_segments") or [])

            assistant_msg = self.svc._insert_message(
                session_id=sid,
                turn_index=int(req.get("turn_index") or 1),
                role="assistant",
                task_mode=str(req.get("task_mode") or "auto"),
                response_type="answer",
                content=content_md,
                reasoning=combined_reasoning,
                reasoning_ms=float(checkpoint.get("reasoning_ms") or 0.0),
                steps=list(checkpoint.get("steps_trace") or []),
                citations=[],
                clarification={},
                next_steps=next_steps_list,
                result_table={},
                proposal_id="",
                provider_name=used_provider_name or provider_id,
                model=used_model or model,
                total_elapsed_ms=total_ms,
            )

            with self.ctx.store.module_db(scope="private") as conn:
                conn.execute(
                    "UPDATE ai_assistant_sessions SET updated_at = ? WHERE session_id = ?",
                    (_now_str(), sid),
                )
            self.svc._compact_rolling_summary_if_needed(sid)

            completed_run = self.svc.update_run(
                run_id,
                status="completed",
                plan=plan,
                checkpoint=checkpoint,
                progress_pct=100,
                current_stage="目标已达成",
                assistant_message_id=int(assistant_msg["id"]),
                finished_at=_now_str(),
            )
            updated_session = self.svc.get_session_detail(sid)
            user_msg_obj = next(
                (
                    m
                    for m in (updated_session.get("messages") or [])
                    if int(m.get("id") or 0) == int(run_rec.get("user_message_id") or 0)
                ),
                None,
            )

            self._emit(
                run_id,
                "done",
                {
                    "type": "done",
                    "ok": True,
                    "run": completed_run,
                    "session": updated_session,
                    "user_message": user_msg_obj,
                    "assistant_message": assistant_msg,
                    "cancelled": False,
                },
            )

        except Exception as exc:
            safe_err = redact_private_storage_paths(str(exc) or "后台 Goal 任务执行失败", ctx=self.ctx)
            try:
                has_progress = bool(checkpoint.get("completed_stage_ids"))
                final_status = "paused_recoverable" if has_progress else "failed"
                updated = self.svc.update_run(
                    run_id,
                    status=final_status,
                    plan=_sanitize_plan_for_storage(plan) if plan else None,
                    checkpoint=checkpoint if checkpoint else None,
                    error_message=safe_err,
                    current_stage="中断可续跑" if has_progress else "执行失败",
                    finished_at="" if has_progress else _now_str(),
                )
                if has_progress:
                    self._emit(
                        run_id,
                        "run_paused",
                        {
                            "type": "run_paused",
                            "run_id": run_id,
                            "status": "paused_recoverable",
                            "reason": safe_err,
                            "plan": _sanitize_plan_for_storage(plan),
                            "checkpoint": checkpoint,
                            "run": updated,
                        },
                    )
                else:
                    self._emit(
                        run_id,
                        "error",
                        {
                            "type": "error",
                            "ok": False,
                            "run_id": run_id,
                            "error": safe_err,
                            "run": updated,
                        },
                    )
            except Exception:
                pass
        finally:
            self.hub.unregister_run(run_id)
            self.hub.notify_stream_close(run_id)

    def _invoke_stage_stream(
        self,
        *,
        run_id: str,
        messages: List[Dict[str, str]],
        provider_id: str,
        model: str,
        thinking_mode: str,
        step_no: int,
        cancel_ev: threading.Event,
        checkpoint: Dict[str, Any],
        emit_content_deltas: bool = False,
    ) -> Tuple[str, str, float, str, str, bool]:
        think_started = False
        think_ended = False
        t_think0 = 0.0
        think_ms = 0.0
        used_prov = ""
        used_mdl = ""
        full_content = ""
        full_reasoning = ""
        was_cancelled = False

        for ev in self.ctx.llm_hub.chat_completion_stream(
            messages=messages,
            provider_id=provider_id,
            model=model,
            temperature=0.1,
            timeout_sec=120.0,
            thinking_mode=thinking_mode,
            cancel_check=cancel_ev.is_set,
            retry_on_transient=True,
            max_retries=3,
            auto_continue_on_length=True,
            max_continuations=3,
        ):
            ev_type = ev.get("type")
            if ev_type == "retry_wait":
                checkpoint["retry_count"] = int(checkpoint.get("retry_count") or 0) + 1
                self._emit(
                    run_id,
                    "retry_wait",
                    {
                        "type": "retry_wait",
                        "step": step_no,
                        "attempt": ev.get("attempt", 1),
                        "max_retries": ev.get("max_retries", 3),
                        "wait_sec": ev.get("wait_sec", 2.0),
                        "reason": ev.get("reason", "响应超时"),
                        "shed_load": bool(ev.get("shed_load")),
                        "detail": str(ev.get("detail") or "供应商响应超时，正在自动退避重连..."),
                    },
                )
            elif ev_type == "status":
                self._emit(
                    run_id,
                    "status",
                    {
                        "type": "status",
                        "stage": str(ev.get("stage") or "推理中"),
                        "detail": str(ev.get("detail") or ""),
                    },
                )
            elif ev_type == "reasoning":
                delta = str(ev.get("delta") or "")
                if delta:
                    if not think_started:
                        think_started = True
                        t_think0 = time.perf_counter()
                        self._emit(run_id, "thinking_start", {"type": "thinking_start", "step": step_no})
                    self._emit(
                        run_id,
                        "thinking_delta",
                        {"type": "thinking_delta", "step": step_no, "delta": delta},
                    )
            elif ev_type == "content":
                if think_started and not think_ended:
                    think_ended = True
                    think_ms = round((time.perf_counter() - t_think0) * 1000.0, 1)
                    self._emit(
                        run_id,
                        "thinking_end",
                        {"type": "thinking_end", "step": step_no, "elapsed_ms": think_ms},
                    )
                delta = str(ev.get("delta") or "")
                if delta and emit_content_deltas:
                    self._emit(run_id, "content_delta", {"type": "content_delta", "delta": delta})
            elif ev_type == "done":
                if think_started and not think_ended:
                    think_ended = True
                    think_ms = round((time.perf_counter() - t_think0) * 1000.0, 1)
                    self._emit(
                        run_id,
                        "thinking_end",
                        {"type": "thinking_end", "step": step_no, "elapsed_ms": think_ms},
                    )
                used_prov = str(ev.get("provider_name") or "")
                used_mdl = str(ev.get("model") or "")
                full_content = str(ev.get("content") or "")
                full_reasoning = str(ev.get("reasoning") or "")
                was_cancelled = bool(ev.get("cancelled"))

        return full_content, full_reasoning, think_ms, used_prov, used_mdl, was_cancelled

    @staticmethod
    def _build_remedial_stage(
        *,
        remedial_round: int,
        next_index: int,
        missed_findings: List[Dict[str, Any]],
        missing_stmt_ids: List[str],
        att_by_id: Dict[str, Dict[str, Any]],
        use_compact: bool,
    ) -> Dict[str, Any]:
        target_aid = ""
        target_fname = ""
        line_nums: List[int] = []
        fids: List[str] = []
        sids: List[str] = list(missing_stmt_ids)

        for f in missed_findings[:12]:
            fids.append(f["finding_id"])
            if f.get("stmt_id") and f["stmt_id"] not in sids:
                sids.append(f["stmt_id"])
            if f.get("line"):
                line_nums.append(int(f["line"]))
            if not target_aid and f.get("attachment_id"):
                target_aid = str(f["attachment_id"])
                target_fname = str(f.get("filename") or "")

        if not target_aid and att_by_id:
            first_att = next(iter(att_by_id.values()))
            target_aid = str(first_att.get("attachment_id") or "")
            target_fname = str(first_att.get("filename") or "")

        s_ln = min(line_nums) if line_nums else 1
        e_ln = max(line_nums) if line_nums else s_ln
        lbl_parts = []
        if fids:
            lbl_parts.append(f"补核 {len(fids)} 项隐患({','.join(fids[:3])})")
        if missing_stmt_ids:
            lbl_parts.append(f"补查 {len(missing_stmt_ids)} 条语句")
        title = f"补救核验 #{remedial_round} · {' / '.join(lbl_parts) or '完整性复核'}"

        return {
            "stage_id": f"remedial_{remedial_round}",
            "index": next_index,
            "title": title[:72],
            "attachment_id": target_aid,
            "filename": target_fname,
            "start_line": s_ln,
            "end_line": e_ln,
            "stmt_ids": sids[:20],
            "target_tables": [],
            "finding_ids": fids,
            "estimated_tokens": 3500,
            "use_compact_view": use_compact,
            "status": "pending",
            "kind": "remedial",
        }

    @staticmethod
    def _build_stage_messages(
        *,
        stage: Dict[str, Any],
        clean_prompt: str,
        dialect: str,
        att_by_id: Dict[str, Dict[str, Any]],
        finding_by_id: Dict[str, Dict[str, Any]],
        checkpoint: Dict[str, Any],
        lineage_brief: str,
        force_compact: bool = False,
        max_code_tokens: int = 0,
    ) -> List[Dict[str, str]]:
        is_remedial = stage.get("kind") == "remedial"
        sys_prompt = (
            f"你是 Data.O.G 首席数据库研发与代码审计专家（目标方言 `{dialect}`）。\n"
            f"当前处于 🎯 Goal 深度模式的【{'定向补救核验阶段' if is_remedial else '甜蜜区分段分析阶段'}】。\n"
            "硬性要求：\n"
            "1. 严禁遗漏【本阶段必查静态隐患】中的任何一项！必须对每一个 `[Fxxx]` 给出明确判定（`confirmed` 确认问题+修复建议，或 `false_positive` 误报+依据）。\n"
            "2. 所有结论必须绑定精确真实行号 `Lxxx` 与实际表名/语句号 `Sxxx`，严禁编造。\n"
            "3. 请优先输出单个结构化 JSON 对象：\n"
            '{"summary":"本阶段核心结论(100字内)","covered_stmts":["S001"],"finding_verdicts":{"F001":{"status":"confirmed","reason":"具体原因","fix":"修复方案"}},"confirmed_issues":["Lxxx 问题描述"],"false_positives":["Lxxx 误报说明"],"lineage_notes":["关键临时表/目标表加工逻辑"],"open_questions":[]}'
        )

        # 压缩前序只读账本
        ledger = checkpoint.get("ledger") or {}
        prev_sums = ledger.get("stage_summaries") or []
        ledger_lines: List[str] = []
        if prev_sums:
            ledger_lines.append("【前序阶段只读账本（勿重复分析，注意跨段临时表衔接）】")
            for ps in prev_sums[-8:]:
                ledger_lines.append(f"- {ps.get('title')}: {ps.get('summary')}")
        if ledger.get("lineage_notes"):
            ledger_lines.append("已识别血缘要点: " + "；".join(ledger["lineage_notes"][-6:]))
        ledger_block = ("\n".join(ledger_lines)[-1800:] + "\n\n") if ledger_lines else ""

        # 本阶段必查静态隐患
        stage_fids = [str(x).upper() for x in (stage.get("finding_ids") or [])]
        f_lines: List[str] = []
        for fid in stage_fids:
            f = finding_by_id.get(fid)
            if f:
                f_lines.append(
                    f"- [{f['finding_id']}][{f['severity']}][{f['code']}] "
                    f"{f.get('stmt_id') or ''} L{f['line']}: {f['title']} — {f['detail']}"
                )
        findings_block = (
            "【本阶段必查静态隐患（必须逐一在 finding_verdicts 中给出判定！）】\n"
            + "\n".join(f_lines)
            + "\n\n"
            if f_lines
            else ""
        )

        # 提取本阶段精确源码
        code_block = ""
        aid = str(stage.get("attachment_id") or "")
        att = att_by_id.get(aid) or (next(iter(att_by_id.values())) if att_by_id else None)
        if att is not None and att.get("raw_text"):
            raw_text = str(att["raw_text"])
            all_lines = raw_text.splitlines()
            an = att.get("analysis") if isinstance(att.get("analysis"), dict) else {}
            sids = list(stage.get("stmt_ids") or [])

            if (is_remedial or stage.get("kind") == "lineage_slice") and (sids or stage_fids):
                # 补救/血缘切片阶段：精准抽取相关语句与隐患周边行
                pieces: List[str] = []
                extracted_ranges: Set[Tuple[int, int]] = set()
                for sid_item in sids[:20]:
                    ext = extract_statement_source(an, raw_text, sid_item, compact=force_compact)
                    if ext:
                        rk = (ext["start_line"], ext["end_line"])
                        if rk not in extracted_ranges:
                            extracted_ranges.add(rk)
                            pieces.append(f"-- 语句 {sid_item} (L{rk[0]}-L{rk[1]})\n{ext['content']}")
                for fid in stage_fids:
                    f = finding_by_id.get(fid)
                    if not f:
                        continue
                    fln = int(f.get("line") or 1)
                    if any(a <= fln <= b for a, b in extracted_ranges):
                        continue
                    w_s = max(1, fln - 12)
                    w_e = min(len(all_lines), fln + 15)
                    extracted_ranges.add((w_s, w_e))
                    seg = "\n".join(all_lines[w_s - 1 : w_e])
                    numbered = (
                        compact_sql_for_llm(seg, start_line=w_s)["text"]
                        if force_compact
                        else format_with_line_numbers(seg, start_line=w_s)
                    )
                    pieces.append(f"-- 隐患 {fid} 上下文 (L{w_s}-L{w_e})\n{numbered}")
                code_block = "\n\n".join(pieces)
            else:
                s_ln = max(1, int(stage.get("start_line") or 1))
                e_ln = min(len(all_lines), max(s_ln, int(stage.get("end_line") or len(all_lines))))
                seg_raw = "\n".join(all_lines[s_ln - 1 : e_ln])
                if force_compact and att.get("file_kind") == "sql":
                    code_block = compact_sql_for_llm(seg_raw, start_line=s_ln)["text"]
                else:
                    code_block = format_with_line_numbers(seg_raw, start_line=s_ln)

            if max_code_tokens > 0 and len(code_block) > max_code_tokens * 3:
                code_block = code_block[: max_code_tokens * 3] + "\n-- ... [已按减载预算截断尾段] ..."

        brief_sec = f"【全局静态血缘图纸参考】\n{lineage_brief}\n\n" if lineage_brief else ""
        code_sec = f"【本阶段待审源码（严格保留真实行号）】\n```sql\n{code_block}\n```" if code_block else ""

        user_msg = (
            f"用户总体目标：{clean_prompt}\n\n"
            f"{brief_sec}{ledger_block}{findings_block}"
            f"当前阶段：{stage.get('title') or stage.get('stage_id')}\n"
            f"{code_sec}"
        )
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ]

    @staticmethod
    def _build_synthesis_messages(
        *,
        clean_prompt: str,
        dialect: str,
        plan: Dict[str, Any],
        checkpoint: Dict[str, Any],
        lineage_brief: str,
        required_hm_findings: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        ledger = checkpoint.get("ledger") or {}
        verdicts = checkpoint.get("finding_verdicts") or {}
        v_sum = checkpoint.get("verification_summary") or {}

        stage_lines = [
            f"- **{s.get('title')}**: {s.get('summary')}"
            for s in (ledger.get("stage_summaries") or [])
        ]
        issue_lines = [f"- {x}" for x in (ledger.get("confirmed_issues") or [])[:30]]
        fp_lines = [f"- {x}" for x in (ledger.get("false_positives") or [])[:15]]
        lin_lines = [f"- {x}" for x in (ledger.get("lineage_notes") or [])[:20]]

        verdict_lines: List[str] = []
        for f in required_hm_findings:
            fid = str(f["finding_id"]).upper()
            v = verdicts.get(fid)
            if v:
                verdict_lines.append(
                    f"- [{fid}][{f['severity']}][{f['code']}] L{f['line']} {f['title']} => "
                    f"判定={v.get('status')} | 说明={v.get('reason')} | 修复={v.get('fix')}"
                )
            else:
                verdict_lines.append(
                    f"- [{fid}][{f['severity']}][{f['code']}] L{f['line']} {f['title']} => [待人工复核]"
                )

        sys_prompt = (
            f"你是 Data.O.G 首席数据库架构师（目标方言 `{dialect}`）。\n"
            "请基于以下全部已验收阶段账本、静态血缘图纸与逐项隐患核验结果，输出一份完整、专业、可直接落地的全景 Markdown 分析报告。\n"
            "报告结构要求：\n"
            "1. **总体执行摘要与数据血缘链路**（清晰交代源表 → 临时表 → 目标表加工路径）；\n"
            "2. **高危与中危隐患逐项核验清单**（Markdown 表格，列含：编号/级别/精确行号 `Lxxx`/判定结果/风险影响/修复建议）；\n"
            "3. **分阶段关键逻辑解读与性能优化/方言兼容改写 SQL**；\n"
            "4. 仅输出纯净 Markdown 正文，可在末尾附可选 ```dataog_meta {\"next_steps\":[...]} ``` 建议按钮。"
        )
        user_content = (
            f"用户原始目标：{clean_prompt}\n"
            f"执行策略：{plan.get('strategy_label')} | 验收状态：{v_sum.get('detail', '已完成')}\n\n"
            + (f"## 静态血缘与结构图纸\n{lineage_brief}\n\n" if lineage_brief else "")
            + ("## 各阶段分析账本\n" + "\n".join(stage_lines) + "\n\n" if stage_lines else "")
            + ("## 静态隐患逐项核验结果\n" + "\n".join(verdict_lines) + "\n\n" if verdict_lines else "")
            + ("## 确认问题明细\n" + "\n".join(issue_lines) + "\n\n" if issue_lines else "")
            + ("## 排除误报说明\n" + "\n".join(fp_lines) + "\n\n" if fp_lines else "")
            + ("## 血缘与业务要点\n" + "\n".join(lin_lines) + "\n\n" if lin_lines else "")
        )
        return [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ]
