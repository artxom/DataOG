"""Data.O.G v3.0 智能数据助手后端独立路由 (/api/modules/ai_assistant).

严格遵守 3.0 模块隔离铁律：仅依赖 `core.platform` 公共底座，与其它 `modules/*` 零私有互引。
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import (
    extract_task_id,
    file_download_response,
    json_error,
    run_tracked_task,
)
from core.platform.storage_paths import redact_private_storage_paths
from modules.ai_assistant.attachments import (
    MAX_ATTACHMENTS_PER_TURN,
    MAX_TEXT_BYTES,
    get_attachment_limits,
)
from modules.ai_assistant.goal_runner import (
    TERMINAL_RUN_STATUSES,
    GoalRunner,
    get_run_runtime_hub,
)
from modules.ai_assistant.schemas import (
    AssistantChatPayload,
    AssistantExportResultPayload,
    AssistantPastedAttachmentPayload,
    AssistantRunCreatePayload,
    AssistantRunResumePayload,
    AssistantSessionCreatePayload,
    AssistantSessionUpdatePayload,
    ProposalConfirmPayload,
    ProposalRejectPayload,
)
from modules.ai_assistant.service import AiAssistantService, ConfirmTokenRequiredError

router = APIRouter(prefix="/api/modules/ai_assistant", tags=["ai_assistant"])


@router.get("/dialects")
def api_list_assistant_dialects(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "dialects": ctx.knowledge_hub.list_supported_dialects(),
        "llm_models": ctx.llm_hub.list_available_models(),
        "knowledge_summary": ctx.knowledge_hub.get_summary(),
        "attachment_limits": get_attachment_limits(),
    }


@router.post("/attachments/upload")
async def api_upload_assistant_attachment(
    file: UploadFile = File(...),
    session_id: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    raw_bytes = await file.read(MAX_TEXT_BYTES + 1024)
    try:
        att = AiAssistantService(ctx).upload_attachment_bytes(
            filename=file.filename or "attachment.txt",
            raw_bytes=raw_bytes,
            session_id=session_id,
        )
        return {"ok": True, "attachment": att}
    except Exception as exc:
        return json_error(str(exc), 400)


@router.post("/attachments/text")
def api_create_pasted_attachment(
    payload: AssistantPastedAttachmentPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        att = AiAssistantService(ctx).create_pasted_attachment(
            content=payload.content,
            filename=payload.filename,
            file_hint=payload.file_hint,
            session_id=payload.session_id,
        )
        return {"ok": True, "attachment": att}
    except Exception as exc:
        return json_error(str(exc), 400)


@router.get("/attachments/{attachment_id}")
def api_get_assistant_attachment(
    attachment_id: str,
    include_chunks: bool = Query(default=True),
    ctx: PlatformContext = Depends(get_platform_context),
):
    att = AiAssistantService(ctx).get_attachment(
        attachment_id, include_raw=False, include_chunks=include_chunks
    )
    if att is None:
        return json_error("未找到指定的附件或附件已过期清理", 404)
    return {"ok": True, "attachment": att}


@router.delete("/attachments/{attachment_id}")
def api_delete_assistant_attachment(
    attachment_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    if not AiAssistantService(ctx).delete_attachment(attachment_id):
        return json_error("未找到要移除的附件", 404)
    return {"ok": True, "message": "附件已移除"}


@router.get("/sessions")
def api_list_assistant_sessions(
    connection_id: Optional[str] = Query(default=None),
    dialect: Optional[str] = Query(default=None),
    limit: int = Query(default=80, ge=1, le=200),
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = AiAssistantService(ctx)
    return {
        "ok": True,
        "sessions": svc.list_sessions(
            connection_id=connection_id,
            dialect=dialect,
            limit=limit,
        ),
        "llm_models": ctx.llm_hub.list_available_models(),
    }


@router.post("/sessions")
def api_create_assistant_session(
    payload: AssistantSessionCreatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = AiAssistantService(ctx)
    try:
        created = svc.create_session(
            title=payload.title,
            work_mode=payload.work_mode,
            connection_id=payload.connection_id,
            dialect=payload.dialect,
            schema_name=payload.schema_name,
            focus_tables=payload.focus_tables,
            task_mode=payload.task_mode,
            provider_id=payload.provider_id,
            model=payload.model,
            thinking_mode=payload.thinking_mode,
        )
        return {
            "ok": True,
            "session": created,
            "sessions": svc.list_sessions(),
        }
    except Exception as exc:
        return json_error(str(exc), 400)


@router.get("/sessions/{session_id}")
def api_get_assistant_session(
    session_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    detail = AiAssistantService(ctx).get_session_detail(session_id)
    if detail is None:
        return json_error("未找到指定的智能助手会话", 404)
    return {"ok": True, "session": detail}


@router.put("/sessions/{session_id}")
@router.patch("/sessions/{session_id}")
def api_update_assistant_session(
    session_id: str,
    payload: AssistantSessionUpdatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    updated = AiAssistantService(ctx).update_session(
        session_id,
        title=payload.title,
        work_mode=payload.work_mode,
        connection_id=payload.connection_id,
        dialect=payload.dialect,
        schema_name=payload.schema_name,
        focus_tables=payload.focus_tables,
        task_mode=payload.task_mode,
        provider_id=payload.provider_id,
        model=payload.model,
        thinking_mode=payload.thinking_mode,
    )
    if updated is None:
        return json_error("未找到指定的智能助手会话", 404)
    return {"ok": True, "session": updated}


@router.delete("/sessions/{session_id}")
def api_delete_assistant_session(
    session_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = AiAssistantService(ctx)
    active = svc.get_active_run_for_session(session_id)
    if active and active.get("run_id"):
        get_run_runtime_hub(ctx).request_cancel(str(active["run_id"]))
    if not svc.delete_session(session_id):
        return json_error("未找到要删除的智能助手会话", 404)
    return {
        "ok": True,
        "sessions": svc.list_sessions(),
        "message": "会话已删除",
    }


@router.get("/sessions/{session_id}/active-run")
def api_get_session_active_run(
    session_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    runner = GoalRunner(ctx)
    active_run = runner.svc.get_active_run_for_session(session_id)
    if active_run is not None:
        active_run = runner.reconcile_orphaned_run(active_run)
    return {
        "ok": True,
        "run": active_run,
    }


@router.post("/runs")
def api_create_assistant_run(
    payload: AssistantRunCreatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        res = GoalRunner(ctx).start_new_run(payload.model_dump())
        return res
    except Exception as exc:
        return json_error(redact_private_storage_paths(str(exc), ctx=ctx), 400)


@router.get("/runs/{run_id}")
def api_get_assistant_run(
    run_id: str,
    include_events: bool = Query(default=True),
    after_event_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=2000),
    ctx: PlatformContext = Depends(get_platform_context),
):
    runner = GoalRunner(ctx)
    run = runner.svc.get_run(run_id)
    if run is None:
        return json_error("未找到指定的后台分析任务", 404)
    run = runner.reconcile_orphaned_run(run) or run
    events = (
        runner.svc.list_run_events(run_id, after_event_id=after_event_id, limit=limit)
        if include_events
        else []
    )
    return {
        "ok": True,
        "run": run,
        "events": events,
    }


@router.get("/runs/{run_id}/events")
async def api_stream_assistant_run_events(
    run_id: str,
    request: Request,
    after_event_id: int = Query(default=0, ge=0),
    ctx: PlatformContext = Depends(get_platform_context),
):
    runner = GoalRunner(ctx)
    svc = runner.svc
    init_run = svc.get_run(run_id)
    if init_run is None:
        return json_error("未找到指定的后台分析任务", 404)
    # 若该任务因进程重启孤悬为 running/probing/planning，先对账为 paused_recoverable 防止 SSE 永久挂起
    runner.reconcile_orphaned_run(init_run)

    hdr_last_id = 0
    raw_hdr = (request.headers.get("last-event-id") or "").strip()
    if raw_hdr.isdigit():
        hdr_last_id = int(raw_hdr)
    cursor_id = max(int(after_event_id or 0), hdr_last_id)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()
    hub = get_run_runtime_hub(ctx)
    # 必须先订阅实时通道再读取数据库历史事件，杜绝边界漏帧
    hub.subscribe(run_id, loop, queue)

    async def _sse_generator():
        last_sent_id = cursor_id
        last_event_was_terminal = False

        def _format_db_events(rows):
            nonlocal last_sent_id, last_event_was_terminal
            frames = []
            for ev in rows:
                ev_id = int(ev.get("event_id") or 0)
                if ev_id <= last_sent_id:
                    continue
                last_sent_id = ev_id
                ev_type = str(ev.get("event_type") or "message")
                ev_payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                frames.append(
                    f"id: {ev_id}\nevent: {ev_type}\ndata: {json.dumps(ev_payload, ensure_ascii=False)}\n\n"
                )
                if ev_type in ("done", "run_paused", "run_cancelled", "error"):
                    last_event_was_terminal = True
                elif ev_type == "stage_start" or (ev_type == "status" and ev_payload.get("resumed")):
                    last_event_was_terminal = False
            return frames

        def _drain_db_frames():
            """分页拉取数据库内所有未推事件，确保超过 2000 帧的超长任务也能零截断补齐."""
            frames_all = []
            while True:
                batch = svc.list_run_events(run_id, after_event_id=last_sent_id, limit=1000)
                if not batch:
                    break
                frames_all.extend(_format_db_events(batch))
                if len(batch) < 1000:
                    break
            return frames_all

        try:
            for frame_str in _drain_db_frames():
                yield frame_str

            cur_run = runner.reconcile_orphaned_run(svc.get_run(run_id))
            if cur_run is None:
                return
            cur_status = str(cur_run.get("status") or "")
            if cur_status in TERMINAL_RUN_STATUSES and (
                last_event_was_terminal or not hub.is_running(run_id)
            ):
                # 二次追平窗口内刚落盘的尾部事件（如 worker 在首次查库与 unregister_run 之间写入的 done）
                for frame_str in _drain_db_frames():
                    yield frame_str
                return

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    latest = runner.reconcile_orphaned_run(svc.get_run(run_id))
                    if latest is None or (
                        str(latest.get("status") or "") in TERMINAL_RUN_STATUSES
                        and not hub.is_running(run_id)
                    ):
                        for frame_str in _drain_db_frames():
                            yield frame_str
                        break
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    for frame_str in _drain_db_frames():
                        yield frame_str
                    break
                ev_id = int(item.get("event_id") or 0)
                if ev_id > 0 and ev_id <= last_sent_id:
                    continue
                if ev_id > last_sent_id:
                    last_sent_id = ev_id
                ev_type = str(
                    item.get("event_type")
                    or (item.get("payload") or {}).get("type")
                    or "message"
                )
                ev_payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
                id_prefix = f"id: {ev_id}\n" if ev_id > 0 else ""
                yield f"{id_prefix}event: {ev_type}\ndata: {json.dumps(ev_payload, ensure_ascii=False)}\n\n"
                if ev_type in ("done", "run_paused", "run_cancelled", "error"):
                    break
        finally:
            # 仅退订当前 SSE 连接，绝不取消后台持续推进中的 GoalRunner 任务
            hub.unsubscribe(run_id, loop, queue)

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/resume")
def api_resume_assistant_run(
    run_id: str,
    payload: Optional[AssistantRunResumePayload] = Body(default=None),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        extra_steps = int(payload.extra_steps) if payload is not None else 15
        return GoalRunner(ctx).resume_run(run_id, extra_steps=extra_steps)
    except KeyError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        return json_error(redact_private_storage_paths(str(exc), ctx=ctx), 400)


@router.post("/runs/{run_id}/cancel")
def api_cancel_assistant_run(
    run_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return GoalRunner(ctx).cancel_run(run_id)
    except KeyError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        return json_error(redact_private_storage_paths(str(exc), ctx=ctx), 400)


@router.post("/chat")
async def api_assistant_chat(
    payload: AssistantChatPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="Data.O.G 智能分析探索",
        start_detail="正在校验模型与数据源上下文...",
        fn=AiAssistantService(ctx).chat_turn,
        kwargs={
            "session_id": payload.session_id,
            "prompt": payload.prompt,
            "work_mode": payload.work_mode,
            "connection_id": payload.connection_id,
            "dialect": payload.dialect,
            "schema_name": payload.schema_name,
            "focus_tables": payload.focus_tables,
            "task_mode": payload.task_mode,
            "provider_id": payload.provider_id,
            "model": payload.model,
            "thinking_mode": payload.thinking_mode,
            "max_steps": payload.max_steps,
            "sample_limit": payload.sample_limit,
            "clarification_reply": payload.clarification_reply,
            "attachment_ids": payload.attachment_ids,
            "large_payload_strategy": payload.large_payload_strategy,
            "batch_start_chunk": payload.batch_start_chunk,
            "batch_max_chunks": payload.batch_max_chunks,
            "run_mode": payload.run_mode,
        },
        inject_progress_cb=True,
        inject_cancel_check=True,
        finish_stage="分析完成",
        detail_fn=lambda r: (
            f"已完成分析（共 {len(((r.get('assistant_message') or {}).get('steps') or []))} 步探索）"
        ),
        count_fn=lambda r: (
            len(((r.get("assistant_message") or {}).get("steps") or [])),
            len(((r.get("assistant_message") or {}).get("steps") or [])),
        ),
        response_fn=lambda r: {"ok": True, **r},
    )


@router.post("/chat/stream")
async def api_assistant_chat_stream(
    payload: AssistantChatPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = AiAssistantService(ctx)
    clean_prompt = (payload.prompt or "").strip()
    if not clean_prompt and not payload.attachment_ids:
        return json_error("请输入要咨询或探查的数据库问题，或先粘贴/上传文本/SQL/CSV 文件", 400)

    for raw_aid in (payload.attachment_ids or [])[:MAX_ATTACHMENTS_PER_TURN]:
        aid = str(raw_aid or "").strip()
        if aid and svc.get_attachment(aid, include_raw=False, include_chunks=False) is None:
            return json_error(f"找不到指定附件 `{aid}`（可能已被移除或过期）", 400)

    eff_provider_id = (payload.provider_id or "").strip()
    eff_model = (payload.model or "").strip()
    if payload.session_id:
        existing_sess = svc.get_session_detail(payload.session_id)
        if existing_sess is not None:
            eff_provider_id = (eff_provider_id or existing_sess.get("provider_id") or "").strip()
            eff_model = (eff_model or existing_sess.get("model") or "").strip()

    try:
        ctx.llm_hub.resolve_provider_and_model(
            provider_id=eff_provider_id, model=eff_model, require_ready=True
        )
    except Exception as exc:
        return json_error(redact_private_storage_paths(str(exc), ctx=ctx), 400)

    tid = extract_task_id(request, task_id)
    if tid:
        ctx.task_progress.start(tid, "Data.O.G 智能流式分析", detail="正在连接大模型与上下文...")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()
    cancel_ev = threading.Event()
    progress_cb = ctx.task_progress.make_reporter(tid) if tid else None

    def _is_cancelled() -> bool:
        return cancel_ev.is_set() or bool(tid and ctx.task_progress.is_cancelled(tid))

    def _enqueue(ev: Dict[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, ev)
        except RuntimeError:
            pass

    def _worker() -> None:
        try:
            res = svc.chat_turn(
                session_id=payload.session_id,
                prompt=payload.prompt,
                work_mode=payload.work_mode,
                connection_id=payload.connection_id,
                dialect=payload.dialect,
                schema_name=payload.schema_name,
                focus_tables=payload.focus_tables,
                task_mode=payload.task_mode,
                provider_id=payload.provider_id,
                model=payload.model,
                thinking_mode=payload.thinking_mode,
                max_steps=payload.max_steps,
                sample_limit=payload.sample_limit,
                clarification_reply=payload.clarification_reply,
                attachment_ids=payload.attachment_ids,
                large_payload_strategy=payload.large_payload_strategy,
                batch_start_chunk=payload.batch_start_chunk,
                batch_max_chunks=payload.batch_max_chunks,
                run_mode=payload.run_mode,
                progress_cb=progress_cb,
                event_cb=_enqueue,
                cancel_check=_is_cancelled,
            )
            if tid and not _is_cancelled():
                steps_cnt = len(((res.get("assistant_message") or {}).get("steps") or []))
                ctx.task_progress.finish(
                    tid,
                    stage="分析完成",
                    detail=f"已完成流式分析（共 {steps_cnt} 步探索）",
                    processed=steps_cnt,
                    total=steps_cnt,
                )
        except Exception as exc:
            safe_err = redact_private_storage_paths(str(exc) or "流式分析失败", ctx=ctx)
            if tid and not _is_cancelled():
                ctx.task_progress.fail(tid, safe_err)
            _enqueue({"type": "error", "ok": False, "error": safe_err})
        finally:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except RuntimeError:
                pass

    threading.Thread(target=_worker, name="ai_assistant_stream", daemon=True).start()

    async def _sse_generator():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    break
                ev_name = str(item.get("type") or "message")
                yield f"event: {ev_name}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            cancel_ev.set()

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/proposals/{proposal_id}/confirm")
def api_confirm_assistant_proposal(
    proposal_id: str,
    payload: ProposalConfirmPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = AiAssistantService(ctx)
    try:
        res = svc.confirm_proposal(
            proposal_id,
            edited_sql=payload.edited_sql,
            confirm_token=payload.confirm_token,
            connection_id=payload.connection_id,
            note=payload.note,
        )
        return res
    except ConfirmTokenRequiredError as exc:
        return JSONResponse(
            {
                "ok": False,
                "code": "CONFIRM_TOKEN_REQUIRED",
                "error": str(exc),
                "assessment": exc.assessment,
            },
            status_code=400,
        )
    except KeyError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        return json_error(str(exc), 400)


@router.post("/proposals/{proposal_id}/reject")
def api_reject_assistant_proposal(
    proposal_id: str,
    payload: ProposalRejectPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return AiAssistantService(ctx).reject_proposal(
            proposal_id, reason=payload.reason
        )
    except KeyError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        return json_error(str(exc), 400)


@router.post("/export-result")
def api_export_assistant_result(
    payload: AssistantExportResultPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        data_bytes, media_type = ctx.query_engine.export_result_bytes(
            payload.columns,
            payload.rows,
            fmt=payload.fmt,
            title=payload.title or "DataOG_Result",
        )
        ext = "xlsx" if (payload.fmt or "").lower() in ("xlsx", "excel") else "csv"
        return file_download_response(
            data_bytes,
            f"{payload.title or 'DataOG_Result'}.{ext}",
            media_type=media_type,
            ascii_fallback=f"DataOG_Result.{ext}",
        )
    except Exception as exc:
        return json_error(str(exc), 400)
