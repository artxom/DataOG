"""平台认证授权、启动引导、执行日志与任务进度流子路由."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import json_error as _err
from core.platform.schemas import AuthActivatePayload

router = APIRouter()

# =========================================================================
# 离线激活码与登录鉴权路由 (/api/platform/auth/*，免前置拦截)
# =========================================================================


@router.get("/auth/status")
def api_auth_status(ctx: PlatformContext = Depends(get_platform_context)):
    status = ctx.license_hub.get_auth_status(force_refresh_ip=True)
    return {
        "ok": True,
        "license": status,
        "appearance": ctx.asset_store.get_public_appearance(),
        **status,
    }


@router.post("/auth/activate")
def api_auth_activate(
    payload: AuthActivatePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        status = ctx.license_hub.activate(
            activation_code=payload.activation_code,
            login_mode=payload.login_mode,
        )
        exp_date = status.get("expires_date") or ""
        msg = f"激活成功！当前激活码可用日期至 {exp_date}" if exp_date else "激活成功！"
        resp = JSONResponse(
            content={
                "ok": True,
                "license": status,
                "message": msg,
            }
        )
        resp.set_cookie(
            key="dataog_session",
            value=ctx.license_hub.issue_session_cookie_value(),
            httponly=True,
            samesite="strict",
            path="/",
            max_age=86400,
        )
        return resp
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/auth/lock")
def api_auth_lock(ctx: PlatformContext = Depends(get_platform_context)):
    status = ctx.license_hub.lock()
    resp = JSONResponse(
        content={
            "ok": True,
            "license": status,
            "message": "工作台已安全锁定",
        }
    )
    resp.delete_cookie(key="dataog_session", path="/")
    return resp


@router.get("/bootstrap")
def api_platform_bootstrap(ctx: PlatformContext = Depends(get_platform_context)):
    return {"ok": True, **ctx.build_platform_bootstrap()}



# =========================================================================
# 全局任务与 SQL 日志抽屉接口
# =========================================================================


@router.get("/logs")
def api_platform_logs(
    limit: int = Query(default=150, ge=1, le=1000),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return {"ok": True, "logs": ctx.store.list_logs(limit=limit)}


@router.post("/logs/clear")
def api_platform_clear_logs(ctx: PlatformContext = Depends(get_platform_context)):
    ctx.store.clear_logs()
    return {"ok": True, "logs": []}


@router.get("/import-progress/{progress_id}")
async def api_platform_import_progress(
    progress_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    prog = ctx.import_progress.get(progress_id)
    return JSONResponse(
        content={
            "ok": True,
            "found": prog is not None,
            "progress": prog,
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/tasks/{task_id}")
async def api_platform_task_status(
    task_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    prog = ctx.task_progress.get(task_id)
    return JSONResponse(
        content={
            "ok": True,
            "found": prog is not None,
            "task": prog,
            "progress": prog,
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.post("/tasks/{task_id}/cancel")
async def api_platform_task_cancel(
    task_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    cancelled = ctx.task_progress.cancel(task_id)
    return JSONResponse(
        content={"ok": True, "cancelled": cancelled},
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/tasks/{task_id}/stream")
async def api_platform_task_stream(
    task_id: str,
    request: Request,
    max_wait_ms: int = Query(default=25000, ge=0, le=120000),
    ctx: PlatformContext = Depends(get_platform_context),
):
    async def _event_generator():
        def _encode_event(snap_obj: Optional[dict]) -> str:
            payload = {
                "ok": True,
                "found": snap_obj is not None,
                "task": snap_obj,
                "progress": snap_obj,
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        initial = ctx.task_progress.get(task_id)
        if max_wait_ms == 0 or (
            initial is not None
            and initial.get("status") in ("completed", "failed", "cancelled")
        ):
            yield _encode_event(initial)
            return

        last_seq = 0
        if initial is not None:
            yield _encode_event(initial)
            last_seq = int(initial.get("seq", 1))

        deadline = time.monotonic() + (max_wait_ms / 1000.0)
        while True:
            if await request.is_disconnected():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            snap = await run_in_threadpool(
                ctx.task_progress.wait_for_change,
                task_id,
                last_seq,
                min(0.35, remaining),
            )
            if snap is None:
                if last_seq > 0:
                    break
                rem_after = deadline - time.monotonic()
                if rem_after <= 0:
                    break
                await asyncio.sleep(min(0.05, rem_after))
                continue

            snap_seq = int(snap.get("seq", 1))
            if snap_seq > last_seq:
                yield _encode_event(snap)
                last_seq = snap_seq
                if snap.get("status") in ("completed", "failed", "cancelled"):
                    break
            elif snap.get("status") in ("completed", "failed", "cancelled"):
                break

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


