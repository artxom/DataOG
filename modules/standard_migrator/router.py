"""单库批量落标贯标执行台 API 路由 (/api/modules/standard_migrator)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import JSONResponse

from core.platform.context import PlatformContext, get_platform_context
from modules.standard_migrator.schemas import (
    BindMigratorConnectionPayload,
    ExecuteBatchPayload,
    LoadDbTablesPayload,
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
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw = await file.read()
        return StandardMigratorService(ctx).upload_excel_checklist(
            file.filename or "checklist.xlsx",
            raw,
            default_schema=default_schema,
            default_table=default_table,
        )
    except Exception as exc:
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
