"""AI 只读智能数据探查模块路由 (/api/modules/data_explorer).

严格遵循插件隔离铁律：仅依赖 `core.platform` 底座，与其他 `modules/*` 零私有互引。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import json_error, run_tracked_task
from modules.data_explorer.schemas import (
    AskExplorerPayload,
    CreateExplorerSessionPayload,
    UpdateExplorerSessionPayload,
)
from modules.data_explorer.service import DataExplorerService

router = APIRouter(prefix="/api/modules/data_explorer", tags=["data_explorer"])


@router.get("/sessions")
def api_list_explorer_sessions(
    connection_id: Optional[str] = Query(default=None),
    limit: int = Query(default=60, ge=1, le=200),
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = DataExplorerService(ctx)
    return {
        "ok": True,
        "sessions": svc.list_sessions(connection_id=connection_id, limit=limit),
        "llm_models": ctx.llm_hub.list_available_models(),
    }


@router.post("/sessions")
def api_create_explorer_session(
    payload: CreateExplorerSessionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = DataExplorerService(ctx)
    try:
        created = svc.create_session(
            connection_id=payload.connection_id,
            schema_name=payload.schema_name,
            focus_tables=payload.focus_tables,
            title=payload.title,
            provider_id=payload.provider_id,
            model=payload.model,
        )
        return {
            "ok": True,
            "session": created,
            "sessions": svc.list_sessions(connection_id=payload.connection_id),
        }
    except Exception as exc:
        return json_error(str(exc), 400)


@router.get("/sessions/{session_id}")
def api_get_explorer_session(
    session_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    detail = DataExplorerService(ctx).get_session_detail(session_id)
    if detail is None:
        return json_error("未找到指定的探查会话", 404)
    return {"ok": True, "session": detail}


@router.patch("/sessions/{session_id}")
@router.put("/sessions/{session_id}")
def api_update_explorer_session(
    session_id: str,
    payload: UpdateExplorerSessionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    updated = DataExplorerService(ctx).update_session(
        session_id,
        title=payload.title,
        schema_name=payload.schema_name,
        focus_tables=payload.focus_tables,
    )
    if updated is None:
        return json_error("未找到指定的探查会话", 404)
    return {"ok": True, "session": updated}


@router.delete("/sessions/{session_id}")
def api_delete_explorer_session(
    session_id: str,
    connection_id: Optional[str] = Query(default=None),
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = DataExplorerService(ctx)
    if not svc.delete_session(session_id):
        return json_error("未找到要删除的探查会话", 404)
    return {
        "ok": True,
        "sessions": svc.list_sessions(connection_id=connection_id),
        "message": "探查会话已删除",
    }


@router.post("/explore")
async def api_run_ai_exploration(
    payload: AskExplorerPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="AI 只读智能探查",
        start_detail="正在校验数据源连接与探查沙箱环境...",
        fn=DataExplorerService(ctx).ask_and_explore,
        kwargs={
            "session_id": payload.session_id,
            "connection_id": payload.connection_id,
            "schema_name": payload.schema_name,
            "focus_tables": payload.focus_tables,
            "prompt": payload.prompt,
            "goal_type": payload.goal_type,
            "provider_id": payload.provider_id,
            "model": payload.model,
            "max_steps": payload.max_steps,
            "sample_limit": payload.sample_limit,
        },
        inject_cancel_check=True,
        finish_stage="探查完成",
        detail_fn=lambda r: (
            f"已完成 AI 只读探查（共 {len(((r.get('assistant_message') or {}).get('steps') or []))} 步采样）"
        ),
        count_fn=lambda r: (
            len(((r.get("assistant_message") or {}).get("steps") or [])),
            len(((r.get("assistant_message") or {}).get("steps") or [])),
        ),
        response_fn=lambda r: {"ok": True, **r},
    )
