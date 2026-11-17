"""16:9 跨环境三栏数标比对中心 API 路由 (/api/modules/cross_comparator)."""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from modules.cross_comparator.schemas import (
    BindRoleConnectionPayload,
    PushToMigratorPayload,
    SaveCrossBindingsPayload,
    SaveCrossPairingsPayload,
)
from modules.cross_comparator.service import CrossComparatorService

router = APIRouter(prefix="/api/modules/cross_comparator", tags=["cross_comparator"])


def _err(msg: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status_code)


@router.get("/overview")
def api_cross_overview(
    table_key: Optional[str] = Query(default=None),
    force_refresh: bool = Query(default=False),
    source_schema: Optional[str] = Query(default=None),
    source_table: Optional[str] = Query(default=None),
    target_schema: Optional[str] = Query(default=None),
    target_table: Optional[str] = Query(default=None),
    format_profile: str = Query(default="AUTO"),
    source_format_profile: str = Query(default="AUTO"),
    target_format_profile: str = Query(default="AUTO"),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return CrossComparatorService(ctx).get_overview_state(
            selected_table_key=table_key,
            force_refresh=force_refresh,
            source_schema=source_schema,
            source_table=source_table,
            target_schema=target_schema,
            target_table=target_table,
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
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
    progress_id: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await file.read()
        ctx.import_progress.start(progress_id, "解析跨环境标准与绑定配置")
        reporter = ctx.import_progress.make_reporter(progress_id)
        res = await run_in_threadpool(
            CrossComparatorService(ctx).import_standard_or_bundle,
            file.filename or "standard.xlsx",
            raw,
            progress_cb=reporter,
        )
        ctx.import_progress.finish(progress_id, detail="导入完成")
        return res
    except Exception as exc:
        ctx.import_progress.fail(progress_id, str(exc))
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
            std_table_key=payload.std_table_key or None,
            format_profile=payload.format_profile,
            source_format_profile=payload.source_format_profile,
            target_format_profile=payload.target_format_profile,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/pairings/save")
def api_cross_save_pairings(
    payload: SaveCrossPairingsPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return CrossComparatorService(ctx).save_pairings(
            std_table_key=payload.std_table_key,
            source_schema=payload.source_schema,
            source_table=payload.source_table,
            target_schema=payload.target_schema,
            target_table=payload.target_table,
            pairings=payload.pairings,
            format_profile=payload.format_profile,
            source_format_profile=payload.source_format_profile,
            target_format_profile=payload.target_format_profile,
            force_refresh=payload.force_refresh,
            sync_to_field_bindings=payload.sync_to_field_bindings,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/pairings/template.xlsx")
def api_cross_pairings_template(
    table_key: str = Query(default=""),
    source_schema: str = Query(default=""),
    source_table: str = Query(default=""),
    target_schema: str = Query(default=""),
    target_table: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        content = CrossComparatorService(ctx).generate_pairing_template_bytes(
            std_table_key=table_key,
            source_schema=source_schema,
            source_table=source_table,
            target_schema=target_schema,
            target_table=target_table,
        )
        fn = quote("DataOG_跨环境字段映射铺底模板.xlsx")
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename=cross_pairings_template.xlsx; filename*=UTF-8''{fn}"
            },
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/pairings/upload")
async def api_cross_pairings_upload(
    file: UploadFile = File(...),
    format_profile: str = Form(default="AUTO"),
    source_format_profile: str = Form(default="AUTO"),
    target_format_profile: str = Form(default="AUTO"),
    progress_id: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await file.read()
        ctx.import_progress.start(progress_id, "解析跨环境映射铺底表")
        reporter = ctx.import_progress.make_reporter(progress_id)
        res = await run_in_threadpool(
            CrossComparatorService(ctx).upload_pairings_excel,
            filename=file.filename or "cross_pairings.xlsx",
            content=raw,
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
            progress_cb=reporter,
        )
        ctx.import_progress.finish(
            progress_id,
            detail=res.get("message", "映射铺底完成"),
            processed=int(res.get("imported_count", 0)),
            total=int(res.get("imported_count", 0)),
        )
        return res
    except Exception as exc:
        ctx.import_progress.fail(progress_id, str(exc))
        return _err(str(exc), 400)
