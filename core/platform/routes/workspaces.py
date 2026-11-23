"""平台 Layer 3 团队共享工作区生命周期与引导式增量合并子路由."""

from __future__ import annotations

import sqlite3
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import json_error as _err
from core.platform.schemas import (
    WorkspaceCreatePayload,
    WorkspaceMergeExecutePayload,
    WorkspaceUpdatePayload,
)
from core.platform.storage_paths import redact_private_storage_paths

router = APIRouter()

# =========================================================================
# Layer 3 团队共享工作区与引导式增量合并中心 (WorkspaceManager)
# =========================================================================


def _safe_workspace_err(
    ctx: PlatformContext,
    exc: Exception,
    status_code: int = 400,
) -> JSONResponse:
    """对工作区接口错误做严格脱敏，确保绝不向前端泄露本地私有保险箱物理路径或内部文件名."""
    if isinstance(exc, sqlite3.DatabaseError):
        return _err("工作区文件损坏、结构异常或无法读取，请确认上传了有效的 .dataog.db / .db 文件", status_code)
    if isinstance(exc, OSError):
        return _err("本地工作区存储读写异常，请检查磁盘空间或文件权限后重试", status_code)
    msg = redact_private_storage_paths(str(exc or "操作失败"), ctx=ctx)
    return _err(msg, status_code)


@router.get("/workspaces")
def api_list_workspaces(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "storage_mode": "SEPARATED" if ctx.store.is_split_storage else "COMPAT",
        "active_workspace": ctx.workspace_hub.get_active_workspace_summary(),
        "workspaces": ctx.workspace_hub.list_workspaces(),
    }


@router.post("/workspaces")
def api_create_workspace(
    payload: WorkspaceCreatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        created = ctx.workspace_hub.create_workspace(
            name=payload.name,
            description=payload.description,
            clone_from_workspace_id=payload.clone_from_workspace_id,
            switch_immediately=payload.switch_immediately,
        )
        return {
            "ok": True,
            "workspace": created,
            "active_workspace": ctx.workspace_hub.get_active_workspace_summary(),
            "workspaces": ctx.workspace_hub.list_workspaces(),
            "bootstrap": ctx.build_platform_bootstrap(),
            "message": f"已创建工作区 [{created.get('name', payload.name)}]",
        }
    except LookupError as exc:
        return _safe_workspace_err(ctx, exc, 404)
    except Exception as exc:
        return _safe_workspace_err(ctx, exc, 400)


@router.put("/workspaces/{workspace_id}")
def api_update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        updated = ctx.workspace_hub.update_workspace(
            workspace_id,
            name=payload.name,
            description=payload.description,
        )
        return {
            "ok": True,
            "workspace": updated,
            "active_workspace": ctx.workspace_hub.get_active_workspace_summary(),
            "workspaces": ctx.workspace_hub.list_workspaces(),
            "message": f"工作区 [{updated.get('name', '')}] 信息已更新",
        }
    except LookupError as exc:
        return _safe_workspace_err(ctx, exc, 404)
    except Exception as exc:
        return _safe_workspace_err(ctx, exc, 400)


@router.post("/workspaces/{workspace_id}/switch")
def api_switch_workspace(
    workspace_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        active = ctx.workspace_hub.switch_workspace(workspace_id)
        return {
            "ok": True,
            "active_workspace": active,
            "workspaces": ctx.workspace_hub.list_workspaces(),
            "bootstrap": ctx.build_platform_bootstrap(),
            "message": f"已切换至工作区 [{active.get('name', '')}]",
        }
    except LookupError as exc:
        return _safe_workspace_err(ctx, exc, 404)
    except Exception as exc:
        return _safe_workspace_err(ctx, exc, 400)


@router.delete("/workspaces/{workspace_id}")
def api_delete_workspace(
    workspace_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        ctx.workspace_hub.delete_workspace(workspace_id)
        return {
            "ok": True,
            "active_workspace": ctx.workspace_hub.get_active_workspace_summary(),
            "workspaces": ctx.workspace_hub.list_workspaces(),
            "message": "工作区已删除",
        }
    except LookupError as exc:
        return _safe_workspace_err(ctx, exc, 404)
    except Exception as exc:
        return _safe_workspace_err(ctx, exc, 400)


@router.get("/workspaces/export")
async def api_export_workspace(
    workspace_id: Optional[str] = Query(default=None),
    include_metadata_cache: bool = Query(default=True),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        data, dl_name = await run_in_threadpool(
            ctx.workspace_hub.export_workspace_bytes,
            workspace_id,
            include_metadata_cache=include_metadata_cache,
        )
        encoded_fn = quote(dl_name)
        return Response(
            content=data,
            media_type="application/vnd.sqlite3",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=workspace.dataog.db; filename*=UTF-8''{encoded_fn}"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )
    except LookupError as exc:
        return _safe_workspace_err(ctx, exc, 404)
    except Exception as exc:
        return _safe_workspace_err(ctx, exc, 400)


@router.post("/workspaces/import")
async def api_import_workspace_as_new(
    file: UploadFile = File(...),
    name: str = Form(default=""),
    description: str = Form(default=""),
    switch_immediately: bool = Form(default=True),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw_bytes = await file.read()
        res = await run_in_threadpool(
            ctx.workspace_hub.import_workspace_as_new,
            filename=file.filename or "imported.dataog.db",
            raw_bytes=raw_bytes,
            name=name,
            description=description,
            switch_immediately=switch_immediately,
        )
        return {
            "ok": True,
            **res,
            "active_workspace": ctx.workspace_hub.get_active_workspace_summary(),
            "workspaces": ctx.workspace_hub.list_workspaces(),
            "bootstrap": ctx.build_platform_bootstrap(),
            "message": f"已安全导入工作区 [{res.get('workspace', {}).get('name', '')}]",
        }
    except Exception as exc:
        return _safe_workspace_err(ctx, exc, 400)


@router.post("/workspaces/merge/inspect")
async def api_inspect_workspace_merge(
    file: UploadFile = File(...),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw_bytes = await file.read()
        preview = await run_in_threadpool(
            ctx.workspace_hub.inspect_merge_source,
            filename=file.filename or "teammate.dataog.db",
            raw_bytes=raw_bytes,
        )
        return {"ok": True, **preview}
    except Exception as exc:
        return _safe_workspace_err(ctx, exc, 400)


@router.post("/workspaces/merge/execute")
async def api_execute_workspace_merge(
    payload: WorkspaceMergeExecutePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        res = await run_in_threadpool(
            ctx.workspace_hub.execute_merge,
            merge_token=payload.merge_token,
            selected_modules=payload.selected_modules,
            strategy=payload.strategy,
        )
        sm = res["summary"]
        return {
            "ok": True,
            **res,
            "bootstrap": ctx.build_platform_bootstrap(),
            "message": f"合并完成：新增 {sm['added']} 项，更新 {sm['updated']} 项，跳过 {sm['skipped']} 项",
        }
    except Exception as exc:
        return _safe_workspace_err(ctx, exc, 400)


