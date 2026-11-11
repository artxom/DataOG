"""16:9 跨环境三栏数标比对中心 API 路由 (/api/modules/cross_comparator)."""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.responses import JSONResponse

from core.platform.context import PlatformContext, get_platform_context
from modules.cross_comparator.schemas import (
    BindRoleConnectionPayload,
    PushToMigratorPayload,
    SaveCrossBindingsPayload,
)
from modules.cross_comparator.service import CrossComparatorService

router = APIRouter(prefix="/api/modules/cross_comparator", tags=["cross_comparator"])


def _err(msg: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status_code)


@router.get("/overview")
def api_cross_overview(
    table_key: Optional[str] = Query(default=None),
    force_refresh: bool = Query(default=False),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return CrossComparatorService(ctx).get_overview_state(
            selected_table_key=table_key, force_refresh=force_refresh
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/bind-role")
def api_cross_bind_role(
    payload: BindRoleConnectionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return CrossComparatorService(ctx).bind_role_connection(
            payload.role,
            payload.connection_id,
            active_table_key=payload.active_table_key,
            auto_refresh=payload.auto_refresh,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/bindings/save")
def api_cross_save_bindings(
    payload: SaveCrossBindingsPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return CrossComparatorService(ctx).save_bindings(
            std_table_key=payload.std_table_key,
            source_schema=payload.source_schema,
            source_table=payload.source_table,
            target_schema=payload.target_schema,
            target_table=payload.target_table,
            column_bindings=payload.column_bindings,
            force_refresh=payload.force_refresh,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/standard/upload")
async def api_cross_upload_standard(
    file: UploadFile = File(...),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await file.read()
        return CrossComparatorService(ctx).import_standard_or_bundle(
            file.filename or "standard.xlsx", raw
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/standard/sync-from-migrator")
def api_cross_sync_from_migrator(ctx: PlatformContext = Depends(get_platform_context)):
    try:
        return CrossComparatorService(ctx).sync_standards_from_migrator()
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/standard/clear")
def api_cross_clear_standards(ctx: PlatformContext = Depends(get_platform_context)):
    ctx.store.clear_cross_standards()
    return CrossComparatorService(ctx).get_overview_state()


@router.get("/bindings/export.json")
def api_cross_export_json(ctx: PlatformContext = Depends(get_platform_context)):
    bundle = ctx.store.export_cross_bindings_bundle()
    content = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
    fn = quote("DataOG_跨环境三方数标绑定配置包.json")
    return Response(
        content=content,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=bindings.json; filename*=UTF-8''{fn}"
        },
    )


@router.get("/bindings/export.xlsx")
def api_cross_export_xlsx(
    table_key: Optional[str] = Query(default=None),
    ctx: PlatformContext = Depends(get_platform_context),
):
    content = CrossComparatorService(ctx).export_comparison_excel_bytes(
        std_table_key=table_key or None
    )
    fn = quote("DataOG_跨环境三栏数标比对与绑定报告.xlsx")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=cross_report.xlsx; filename*=UTF-8''{fn}"
        },
    )


@router.post("/push-to-migrator")
def api_cross_push_to_migrator(
    payload: PushToMigratorPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return CrossComparatorService(ctx).push_to_migrator(
            std_table_key=payload.std_table_key or None
        )
    except Exception as exc:
        return _err(str(exc), 400)
