"""平台增量版本升级与补丁管理子路由 (/api/platform/updates/*)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import json_error as _err
from update_bootstrap import MAX_PACKAGE_BYTES, UpdateVerificationError

router = APIRouter()


async def _read_upload_bounded(file: UploadFile) -> bytes:
    raw = await file.read(MAX_PACKAGE_BYTES + 1)
    if len(raw) > MAX_PACKAGE_BYTES:
        raise UpdateVerificationError(
            "SIZE_LIMIT", f"更新包体积超过 {MAX_PACKAGE_BYTES // (1024 * 1024)} MB 安全上限"
        )
    return raw


@router.get("/updates/status")
def api_updates_status(ctx: PlatformContext = Depends(get_platform_context)):
    return {"ok": True, "update": ctx.update_hub.get_status()}


@router.post("/updates/inspect")
async def api_updates_inspect(
    file: UploadFile = File(...),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await _read_upload_bounded(file)
        info = await run_in_threadpool(
            ctx.update_hub.inspect_update_bytes, raw, file.filename or ""
        )
        return info
    except UpdateVerificationError as exc:
        return _err(exc.message, 400)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/updates/upload")
async def api_updates_upload(
    file: UploadFile = File(...),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await _read_upload_bounded(file)
        res = await run_in_threadpool(
            ctx.update_hub.install_update_bytes, raw, file.filename or ""
        )
        return res
    except UpdateVerificationError as exc:
        return _err(exc.message, 400)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/updates/scan")
async def api_updates_scan(ctx: PlatformContext = Depends(get_platform_context)):
    try:
        return await run_in_threadpool(ctx.update_hub.scan_install_dir_updates)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/updates/rollback")
def api_updates_rollback(ctx: PlatformContext = Depends(get_platform_context)):
    try:
        return ctx.update_hub.rollback_to_previous()
    except UpdateVerificationError as exc:
        return _err(exc.message, 400)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/updates/reset-base")
def api_updates_reset_base(ctx: PlatformContext = Depends(get_platform_context)):
    try:
        return ctx.update_hub.reset_to_base_version()
    except Exception as exc:
        return _err(str(exc), 400)


@router.delete("/updates/releases/{version}")
def api_updates_delete_release(
    version: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.update_hub.delete_cached_patch(version)
    except Exception as exc:
        return _err(str(exc), 400)
