"""平台外置应用插件管理子路由 (/api/platform/plugins/*)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import json_error as _err
from core.platform.plugin_package import MAX_PLUGIN_PACKAGE_BYTES, PluginVerificationError

router = APIRouter()


async def _read_plugin_upload_bounded(file: UploadFile) -> bytes:
    raw = await file.read(MAX_PLUGIN_PACKAGE_BYTES + 1)
    if len(raw) > MAX_PLUGIN_PACKAGE_BYTES:
        raise PluginVerificationError(
            "SIZE_LIMIT", f"插件包体积超过 {MAX_PLUGIN_PACKAGE_BYTES // (1024 * 1024)} MB 安全上限"
        )
    return raw


@router.get("/plugins/status")
def api_plugins_status(ctx: PlatformContext = Depends(get_platform_context)):
    return {"ok": True, "plugin_hub": ctx.plugin_hub.get_status()}


@router.post("/plugins/inspect")
async def api_plugins_inspect(
    file: UploadFile = File(...),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await _read_plugin_upload_bounded(file)
        return await run_in_threadpool(
            ctx.plugin_hub.inspect_plugin_bytes, raw, file.filename or ""
        )
    except PluginVerificationError as exc:
        return _err(exc.message, 400)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/plugins/upload")
async def api_plugins_upload(
    file: UploadFile = File(...),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await _read_plugin_upload_bounded(file)
        return await run_in_threadpool(
            ctx.plugin_hub.install_plugin_bytes, raw, file.filename or ""
        )
    except PluginVerificationError as exc:
        return _err(exc.message, 400)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/plugins/scan")
async def api_plugins_scan(ctx: PlatformContext = Depends(get_platform_context)):
    try:
        return await run_in_threadpool(ctx.plugin_hub.scan_install_dir_plugins)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/plugins/{plugin_id}/enable")
def api_plugins_enable(
    plugin_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.plugin_hub.set_plugin_enabled(plugin_id, True)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/plugins/{plugin_id}/disable")
def api_plugins_disable(
    plugin_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.plugin_hub.set_plugin_enabled(plugin_id, False)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/plugins/{plugin_id}/rollback")
def api_plugins_rollback(
    plugin_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.plugin_hub.rollback_plugin(plugin_id)
    except Exception as exc:
        return _err(str(exc), 400)


@router.delete("/plugins/{plugin_id}")
def api_plugins_uninstall(
    plugin_id: str,
    purge_data: bool = Query(default=False),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.plugin_hub.uninstall_plugin(plugin_id, purge_data=purge_data)
    except Exception as exc:
        return _err(str(exc), 400)
