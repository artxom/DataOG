"""AI 只读智能数据探查模块路由 (/api/modules/data_explorer).

严格遵循插件隔离铁律：仅依赖 `core.platform` 底座，与其他 `modules/*` 零私有互引。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from core.platform.context import PlatformContext, get_platform_context
from modules.data_explorer.schemas import (
    AskExplorerPayload,
    CreateExplorerSessionPayload,
    UpdateExplorerSessionPayload,
)
from modules.data_explorer.service import DataExplorerService

router = APIRouter(prefix="/api/modules/data_explorer", tags=["data_explorer"])


def _err(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


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
        return _err(str(exc), 400)


@router.get("/sessions/{session_id}")
def api_get_explorer_session(
    session_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = DataExplorerService(ctx)
    detail = svc.get_session_detail(session_id)
    if detail is None:
        return _err("未找到指定的探查会话", 404)
    return {"ok": True, "session": detail}


@router.patch("/sessions/{session_id}")
@router.put("/sessions/{session_id}")
def api_update_explorer_session(
    session_id: str,
    payload: UpdateExplorerSessionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = DataExplorerService(ctx)
    updated = svc.update_session(
        session_id,
        title=payload.title,
        schema_name=payload.schema_name,
        focus_tables=payload.focus_tables,
    )
    if updated is None:
        return _err("未找到指定的探查会话", 404)
    return {"ok": True, "session": updated}


@router.delete("/sessions/{session_id}")
def api_delete_explorer_session(
    session_id: str,
    connection_id: Optional[str] = Query(default=None),
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = DataExplorerService(ctx)
    ok = svc.delete_session(session_id)
    if not ok:
        return _err("未找到要删除的探查会话", 404)
    return {
        "ok": True,
        "sessions": svc.list_sessions(connection_id=connection_id),
        "message": "探查会话已删除",
    }


@router.post("/explore")
def api_run_ai_exploration(
    payload: AskExplorerPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    svc = DataExplorerService(ctx)
    try:
        res = svc.ask_and_explore(
            session_id=payload.session_id,
            connection_id=payload.connection_id,
            schema_name=payload.schema_name,
            focus_tables=payload.focus_tables,
            prompt=payload.prompt,
            goal_type=payload.goal_type,
            provider_id=payload.provider_id,
            model=payload.model,
            max_steps=payload.max_steps,
            sample_limit=payload.sample_limit,
        )
        return {"ok": True, **res}
    except Exception as exc:
        return _err(str(exc), 400)
