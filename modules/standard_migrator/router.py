"""单库批量落标贯标执行台 API 路由 (/api/modules/standard_migrator)."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.router import extract_task_id
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


async def _run_tracked_task(
    *,
    request: Request,
    task_id: str,
    ctx: PlatformContext,
    title: str,
    start_detail: str,
    fn: Callable[..., Dict[str, Any]],
    args: Tuple[Any, ...] = (),
    kwargs: Optional[Dict[str, Any]] = None,
    default_detail: str = "处理完成",
    detail_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
    count_fn: Optional[Callable[[Dict[str, Any]], Tuple[int, int]]] = None,
) -> Any:
    """统一包装支持 task_id 实时进度与取消生命周期的模块端点."""
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(tid, title, detail=start_detail)
        reporter = ctx.task_progress.make_reporter(tid)
        call_kwargs = dict(kwargs or {})
        call_kwargs["progress_cb"] = reporter
        res = await run_in_threadpool(fn, *args, **call_kwargs)
        if tid and not ctx.task_progress.is_cancelled(tid):
            processed, total = count_fn(res) if count_fn else (0, 0)
            detail = (
                detail_fn(res)
                if detail_fn
                else str(res.get("message") or default_detail)
            )
            ctx.task_progress.finish(
                tid,
                stage="处理完成",
                detail=detail,
                warning=str(res.get("warning") or ""),
                processed=processed,
                total=total,
            )
        return res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.get("/state")
def api_migrator_state(ctx: PlatformContext = Depends(get_platform_context)):
    return StandardMigratorService(ctx).get_workspace_state()


@router.post("/bind-connection")
async def api_migrator_bind_connection(
    payload: BindMigratorConnectionPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await _run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="绑定数据源",
        start_detail=f"正在切换并同步数据源 [{payload.connection_id}]...",
        fn=StandardMigratorService(ctx).bind_connection,
        args=(payload.connection_id,),
        kwargs={"auto_sync": payload.auto_sync},
        default_detail="数据源绑定完成",
    )


@router.post("/sync")
async def api_migrator_sync(
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await _run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="核验目标库状态",
        start_detail="正在比对贯标队列与目标库最新字段结构...",
        fn=StandardMigratorService(ctx).sync_items,
        default_detail="实时核验完成",
        count_fn=lambda r: (len(r.get("items") or []), len(r.get("items") or [])),
    )


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
async def api_migrator_load_from_db(
    payload: LoadDbTablesPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await _run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="从数据库载入表结构",
        start_detail="正在读取目标数据源表与字段定义...",
        fn=StandardMigratorService(ctx).load_tables_from_db,
        kwargs={
            "connection_id": payload.connection_id,
            "schema_name": payload.schema_name,
            "table_name": payload.table_name,
            "max_tables": payload.max_tables,
        },
        default_detail="表结构载入完成",
        count_fn=lambda r: (
            int(r.get("loaded_count", 0) or 0),
            int(r.get("loaded_count", 0) or 0),
        ),
    )


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
async def api_migrator_execute_single(
    item_id: int,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await _run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="执行单字段贯标",
        start_detail=f"正在准备执行字段贯标任务 #{item_id}...",
        fn=StandardMigratorService(ctx).execute_single,
        args=(item_id,),
        default_detail="单字段贯标执行完成",
        count_fn=lambda _r: (1, 1),
    )


@router.post("/execute/batch")
async def api_migrator_execute_batch(
    payload: ExecuteBatchPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await _run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="批量并发贯标",
        start_detail=f"正在调度 {len(payload.item_ids)} 条字段规则并发执行...",
        fn=StandardMigratorService(ctx).execute_batch,
        args=(payload.item_ids,),
        kwargs={
            "max_workers": payload.max_workers,
            "keep_backup": payload.keep_backup,
        },
        default_detail="批量贯标任务执行完毕",
        count_fn=lambda r: (
            int((r.get("batch_result") or {}).get("executed_count", len(payload.item_ids)) or 0),
            int((r.get("batch_result") or {}).get("executed_count", len(payload.item_ids)) or 0),
        ),
    )


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
async def api_migrator_rebuild_execute(
    payload: TableRebuildPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await _run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="整表保序重建",
        start_detail=f"正在准备对 {payload.schema_name}.{payload.table_name} 执行保序重建...",
        fn=StandardMigratorService(ctx).execute_rebuild,
        args=(payload.schema_name, payload.table_name),
        kwargs={
            "item_ids": payload.item_ids,
            "keep_backup": payload.keep_backup,
        },
        default_detail="整表保序重建完成",
        count_fn=lambda r: (
            int((r.get("rebuild_result") or {}).get("valid_count", 0) or 0),
            int((r.get("rebuild_result") or {}).get("valid_count", 0) or 0),
        ),
    )


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
async def api_bindings_table(
    request: Request,
    connection_id: str = Query(...),
    schema_name: str = Query(default=""),
    table_name: str = Query(...),
    format_profile: str = Query(default="AUTO"),
    force_refresh: bool = Query(default=False),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    target_label = f"{schema_name}.{table_name}" if schema_name else table_name
    return await _run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="核验表字段落标",
        start_detail=f"正在加载 {target_label} 结构与数标绑定...",
        fn=StandardMigratorService(ctx).get_table_compliance,
        kwargs={
            "connection_id": connection_id,
            "schema_name": schema_name,
            "table_name": table_name,
            "format_profile": format_profile,
            "force_refresh": force_refresh,
        },
        detail_fn=lambda r: (
            f"已完成 {(r.get('table') or {}).get('schema_name', schema_name)}.{(r.get('table') or {}).get('table_name', table_name)} "
            f"落标核验（已落标 {(r.get('summary') or {}).get('compliant_cols', 0)}/{(r.get('summary') or {}).get('total_cols', 0)} 列）"
        ),
        count_fn=lambda r: (
            int((r.get("summary") or {}).get("total_cols", len(r.get("columns") or [])) or 0),
            int((r.get("summary") or {}).get("total_cols", len(r.get("columns") or [])) or 0),
        ),
    )


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
async def api_bindings_auto_match(
    payload: AutoMatchBindingsPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await _run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="智能预匹配数据标准",
        start_detail="正在读取未绑字段并匹配标准字典...",
        fn=StandardMigratorService(ctx).auto_match_bindings,
        kwargs={
            "connection_id": payload.connection_id,
            "tables": payload.tables,
            "schema_name": payload.schema_name,
            "table_name": payload.table_name,
            "format_profile": payload.format_profile,
        },
        default_detail="智能预匹配完成",
        count_fn=lambda r: (
            int(r.get("checked_tables", 0) or 0),
            int(r.get("checked_tables", 0) or 0),
        ),
    )


@router.post("/bindings/check-compliance")
async def api_bindings_check_compliance(
    payload: CheckCompliancePayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await _run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="检查字段落标状态",
        start_detail="正在检查目标库表字段绑标与落标合规性...",
        fn=StandardMigratorService(ctx).check_compliance,
        kwargs={
            "connection_id": payload.connection_id,
            "tables": payload.tables,
            "format_profile": payload.format_profile,
            "force_refresh": payload.force_refresh,
        },
        default_detail="落标检查完成",
        count_fn=lambda r: (
            int((r.get("aggregate") or {}).get("checked_tables", len(r.get("tables") or [])) or 0),
            int((r.get("aggregate") or {}).get("checked_tables", len(r.get("tables") or [])) or 0),
        ),
    )


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
