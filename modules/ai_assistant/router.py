"""Data.O.G v3.0 智能数据助手后端独立路由 (/api/modules/ai_assistant).

严格遵守 3.0 模块隔离铁律：仅依赖 `core.platform` 公共底座，与其它 `modules/*` 零私有互引。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import file_download_response, json_error, run_tracked_task
from modules.ai_assistant.attachments import MAX_TEXT_BYTES, get_attachment_limits
from modules.ai_assistant.schemas import (
    AssistantChatPayload,
    AssistantExportResultPayload,
    AssistantPastedAttachmentPayload,
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
    if not svc.delete_session(session_id):
        return json_error("未找到要删除的智能助手会话", 404)
    return {
        "ok": True,
        "sessions": svc.list_sessions(),
        "message": "会话已删除",
    }


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
            "max_steps": payload.max_steps,
            "sample_limit": payload.sample_limit,
            "clarification_reply": payload.clarification_reply,
            "attachment_ids": payload.attachment_ids,
            "large_payload_strategy": payload.large_payload_strategy,
            "batch_start_chunk": payload.batch_start_chunk,
            "batch_max_chunks": payload.batch_max_chunks,
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
