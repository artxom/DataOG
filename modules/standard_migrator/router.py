"""单库批量落标贯标执行台 API 路由 (/api/modules/standard_migrator)."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from modules.standard_migrator.schemas import (
    AutoMatchBindingsPayload,
    BindMigratorConnectionPayload,
    CheckCompliancePayload,
    ExecuteBatchPayload,
    LoadDbTablesPayload,
    SaveFieldBindingsPayload,
    StageMigrationFromBindingsPayload,
    TableRebuildPayload,
)
from modules.standard_migrator.service import StandardMigratorService

router = APIRouter(prefix="/api/modules/standard_migrator", tags=["standard_migrator"])


def _err(msg: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status_code)


@router.get("/state")
def api_migrator_state(ctx: PlatformContext = Depends(get_platform_context)):
    return StandardMigratorService(ctx).get_workspace_state()


@router.post("/bind-connection")
def api_migrator_bind_connection(
    payload: BindMigratorConnectionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).bind_connection(
            payload.connection_id, auto_sync=payload.auto_sync
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/sync")
def api_migrator_sync(ctx: PlatformContext = Depends(get_platform_context)):
    try:
        return StandardMigratorService(ctx).sync_items()
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/checklist/upload")
async def api_migrator_upload_checklist(
    file: UploadFile = File(...),
    default_schema: str = Form(""),
    default_table: str = Form(""),
    progress_id: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await file.read()
        ctx.import_progress.start(progress_id, "解析单库贯标改造清单")
        reporter = ctx.import_progress.make_reporter(progress_id)
        res = await run_in_threadpool(
            StandardMigratorService(ctx).upload_excel_checklist,
            file.filename or "checklist.xlsx",
            raw,
            default_schema=default_schema,
            default_table=default_table,
            progress_cb=reporter,
        )
        ctx.import_progress.finish(progress_id, detail=res.get("message", "导入完成"))
        return res
    except Exception as exc:
        ctx.import_progress.fail(progress_id, str(exc))
        return _err(str(exc), 400)


@router.post("/checklist/load-from-db")
def api_migrator_load_from_db(
    payload: LoadDbTablesPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).load_tables_from_db(
            connection_id=payload.connection_id,
            schema_name=payload.schema_name,
            table_name=payload.table_name,
            max_tables=payload.max_tables,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/checklist/clear")
def api_migrator_clear(ctx: PlatformContext = Depends(get_platform_context)):
    return StandardMigratorService(ctx).clear_items()


@router.get("/checklist/template.xlsx")
def api_migrator_template():
    content = StandardMigratorService.generate_template_bytes()
    fn = quote("DataOG_字段贯标标准导入模板.xlsx")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=template.xlsx; filename*=UTF-8''{fn}"
        },
    )


@router.get("/checklist/report.xlsx")
def api_migrator_report(ctx: PlatformContext = Depends(get_platform_context)):
    content = StandardMigratorService(ctx).export_report_bytes()
    fn = quote("DataOG_单库贯标核验与执行报告.xlsx")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=report.xlsx; filename*=UTF-8''{fn}"
        },
    )


@router.post("/execute/item/{item_id}")
def api_migrator_execute_single(
    item_id: int, ctx: PlatformContext = Depends(get_platform_context)
):
    try:
        return StandardMigratorService(ctx).execute_single(item_id)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/execute/batch")
def api_migrator_execute_batch(
    payload: ExecuteBatchPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).execute_batch(
            payload.item_ids,
            max_workers=payload.max_workers,
            keep_backup=payload.keep_backup,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/rebuild/preview")
def api_migrator_rebuild_preview(
    payload: TableRebuildPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).preview_rebuild(
            payload.schema_name,
            payload.table_name,
            item_ids=payload.item_ids,
            keep_backup=payload.keep_backup,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/rebuild/execute")
def api_migrator_rebuild_execute(
    payload: TableRebuildPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).execute_rebuild(
            payload.schema_name,
            payload.table_name,
            item_ids=payload.item_ids,
            keep_backup=payload.keep_backup,
        )
    except Exception as exc:
        return _err(str(exc), 400)


# =========================================================================
# 第 2 步：数据源字段绑定数标 + 批量对照导入 + 三维分级落标检查
# =========================================================================


@router.get("/bindings/overview")
def api_bindings_overview(
    connection_id: str = Query(default=""),
    format_profile: str = Query(default="AUTO"),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).get_bindings_overview(
            connection_id=connection_id,
            format_profile=format_profile,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/bindings/table")
def api_bindings_table(
    connection_id: str = Query(...),
    schema_name: str = Query(default=""),
    table_name: str = Query(...),
    format_profile: str = Query(default="AUTO"),
    force_refresh: bool = Query(default=False),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).get_table_compliance(
            connection_id=connection_id,
            schema_name=schema_name,
            table_name=table_name,
            format_profile=format_profile,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/bindings/save")
def api_bindings_save(
    payload: SaveFieldBindingsPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).save_field_bindings(
            connection_id=payload.connection_id,
            bindings=payload.bindings,
            format_profile=payload.format_profile,
            active_schema=payload.active_schema,
            active_table=payload.active_table,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/bindings/auto-match")
def api_bindings_auto_match(
    payload: AutoMatchBindingsPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).auto_match_bindings(
            connection_id=payload.connection_id,
            tables=payload.tables,
            schema_name=payload.schema_name,
            table_name=payload.table_name,
            format_profile=payload.format_profile,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/bindings/check-compliance")
def api_bindings_check_compliance(
    payload: CheckCompliancePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).check_compliance(
            connection_id=payload.connection_id,
            tables=payload.tables,
            format_profile=payload.format_profile,
            force_refresh=payload.force_refresh,
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/bindings/template.xlsx")
def api_bindings_template(
    connection_id: str = Query(default=""),
    schema_name: str = Query(default=""),
    tables: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        content = StandardMigratorService(ctx).generate_binding_template_bytes(
            connection_id=connection_id,
            schema_name=schema_name,
            tables=tables,
        )
        fn = quote("DataOG_字段与数据标准对照模板.xlsx")
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename=binding_template.xlsx; filename*=UTF-8''{fn}"
            },
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/bindings/upload")
async def api_bindings_upload(
    file: UploadFile = File(...),
    connection_id: str = Form(...),
    default_schema: str = Form(default=""),
    mode: str = Form(default="merge"),
    format_profile: str = Form(default="AUTO"),
    progress_id: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await file.read()
        ctx.import_progress.start(progress_id, "解析表字段与数据标准对照关系")
        reporter = ctx.import_progress.make_reporter(progress_id)
        res = await run_in_threadpool(
            StandardMigratorService(ctx).upload_field_bindings_excel,
            filename=file.filename or "bindings.xlsx",
            content=raw,
            connection_id=connection_id,
            default_schema=default_schema,
            mode=mode,
            format_profile=format_profile,
            progress_cb=reporter,
        )
        ctx.import_progress.finish(
            progress_id,
            detail=res.get("message", "对照关系导入完成"),
            processed=int(res.get("imported_count", 0)),
            total=int(res.get("imported_count", 0)),
        )
        return res
    except Exception as exc:
        ctx.import_progress.fail(progress_id, str(exc))
        return _err(str(exc), 400)


@router.get("/bindings/report.xlsx")
def api_bindings_report(
    connection_id: str = Query(default=""),
    format_profile: str = Query(default="AUTO"),
    tables: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        content = StandardMigratorService(ctx).export_binding_report_bytes(
            connection_id=connection_id,
            format_profile=format_profile,
            tables=tables,
        )
        fn = quote("DataOG_单库字段落标检查报告.xlsx")
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename=binding_report.xlsx; filename*=UTF-8''{fn}"
            },
        )
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/bindings/stage-migration")
def api_bindings_stage_migration(
    payload: StageMigrationFromBindingsPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).stage_migration_from_bindings(
            connection_id=payload.connection_id,
            schema_name=payload.schema_name,
            table_name=payload.table_name,
            column_names=payload.column_names,
            format_profile=payload.format_profile,
            append=payload.append,
        )
    except Exception as exc:
        return _err(str(exc), 400)
