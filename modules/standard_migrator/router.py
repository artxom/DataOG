"""单库批量落标贯标执行台 API 路由 (/api/modules/standard_migrator)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import (
    MEDIA_TYPE_HTML,
    MEDIA_TYPE_XLSX,
    file_download_response,
    json_error,
    run_tracked_task,
    run_tracked_upload,
)
from modules.standard_migrator.schemas import (
    AutoMatchBindingsPayload,
    BindMigratorConnectionPayload,
    CheckCompliancePayload,
    DeleteMigrationItemsPayload,
    ExecuteBatchPayload,
    LoadDbTablesPayload,
    SaveFieldBindingsPayload,
    StageMigrationFromBindingsPayload,
    TableRebuildPayload,
)
from modules.standard_migrator.service import StandardMigratorService

router = APIRouter(prefix="/api/modules/standard_migrator", tags=["standard_migrator"])


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
    return await run_tracked_task(
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
    return await run_tracked_task(
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
    raw = await file.read()
    return await run_tracked_upload(
        ctx=ctx,
        progress_id=progress_id,
        title="解析单库贯标改造清单",
        fn=StandardMigratorService(ctx).upload_excel_checklist,
        args=(file.filename or "checklist.xlsx", raw),
        kwargs={"default_schema": default_schema, "default_table": default_table},
        default_detail="导入完成",
    )


@router.post("/checklist/load-from-db")
async def api_migrator_load_from_db(
    payload: LoadDbTablesPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
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


@router.post("/checklist/delete-items")
def api_migrator_delete_items(
    payload: DeleteMigrationItemsPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).delete_items(payload.item_ids)
    except Exception as exc:
        return json_error(str(exc), 400)


@router.get("/items/{item_id}/sql")
def api_migrator_item_sql(
    item_id: int,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return StandardMigratorService(ctx).preview_item_sql(item_id)
    except Exception as exc:
        return json_error(str(exc), 400)


@router.get("/checklist/template.xlsx")
def api_migrator_template(
    _ctx: PlatformContext = Depends(get_platform_context),
):
    return file_download_response(
        StandardMigratorService.generate_template_bytes(),
        "DataOG_字段贯标标准导入模板.xlsx",
        media_type=MEDIA_TYPE_XLSX,
        ascii_fallback="template.xlsx",
    )


@router.get("/checklist/report.xlsx")
def api_migrator_report(ctx: PlatformContext = Depends(get_platform_context)):
    return file_download_response(
        StandardMigratorService(ctx).export_report_bytes(),
        "DataOG_单库贯标核验与执行报告.xlsx",
        media_type=MEDIA_TYPE_XLSX,
        ascii_fallback="report.xlsx",
    )


@router.post("/execute/item/{item_id}")
async def api_migrator_execute_single(
    item_id: int,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
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
    return await run_tracked_task(
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
            connection_id=payload.connection_id,
        )
    except Exception as exc:
        return json_error(str(exc), 400)


@router.post("/rebuild/execute")
async def api_migrator_rebuild_execute(
    payload: TableRebuildPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
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
            "connection_id": payload.connection_id,
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
        return json_error(str(exc), 400)


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
    return await run_tracked_task(
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
        return json_error(str(exc), 400)


@router.post("/bindings/auto-match")
async def api_bindings_auto_match(
    payload: AutoMatchBindingsPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
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
    eff_scope = (payload.scope or "").strip().lower()
    if eff_scope == "all_schemas":
        task_title = "全库 Schema 刷新并比对落标"
        start_detail = "正在刷新全部 Schema 目录并批量核验字段落标状态..."
    elif eff_scope == "schema":
        sch_lbl = ", ".join(s for s in (payload.schemas or []) if s) or payload.schema_name or "当前 Schema"
        task_title = f"全 Schema 刷新并比对落标 ({sch_lbl})"
        start_detail = f"正在刷新 {sch_lbl} 目录并批量核验字段落标状态..."
    else:
        task_title = "检查字段落标状态"
        start_detail = "正在检查目标库表字段绑标与落标合规性..."

    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title=task_title,
        start_detail=start_detail,
        fn=StandardMigratorService(ctx).check_compliance,
        kwargs={
            "connection_id": payload.connection_id,
            "tables": payload.tables,
            "schemas": payload.schemas,
            "schema_name": payload.schema_name,
            "scope": payload.scope,
            "refresh_catalog": payload.refresh_catalog,
            "only_bound_tables": payload.only_bound_tables,
            "format_profile": payload.format_profile,
            "force_refresh": payload.force_refresh,
        },
        default_detail="落标检查完成",
        count_fn=lambda r: (
            int((r.get("aggregate") or {}).get("checked_tables", len(r.get("tables") or [])) or 0),
            int((r.get("aggregate") or {}).get("checked_tables", len(r.get("tables") or [])) or 0),
        ),
    )


def _build_binding_template_filename(
    tables: str = "",
    schemas: str = "",
    schema_name: str = "",
    scope: str = "",
) -> str:
    def _safe_part(raw: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\s]+', "_", str(raw or "")).strip("._")
        return cleaned[:48]

    tbl_raw = (tables or "").strip()
    eff_scope = (scope or "").strip().lower()
    if tbl_raw:
        first_tbl = _safe_part(tbl_raw.split(",", 1)[0])
        suffix = f"_{first_tbl}" if first_tbl else ""
        return f"DataOG_字段落标映射关系{suffix}_模板.xlsx"
    if eff_scope in ("schema", "all"):
        sch_raw = (schemas or schema_name or "").strip()
        if sch_raw:
            first_sch = _safe_part(sch_raw.split(",", 1)[0])
            suffix = f"_{first_sch}" if first_sch else ""
            return f"DataOG_字段落标映射关系{suffix}_Schema全量模板.xlsx"
        return "DataOG_字段落标映射关系_全库全量模板.xlsx"
    return "DataOG_字段落标映射关系_空白模板.xlsx"


@router.get("/bindings/template.xlsx")
async def api_bindings_template(
    request: Request,
    connection_id: str = Query(default=""),
    schema_name: str = Query(default=""),
    tables: str = Query(default=""),
    schemas: str = Query(default=""),
    scope: str = Query(default=""),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    dl_name = _build_binding_template_filename(
        tables=tables,
        schemas=schemas,
        schema_name=schema_name,
        scope=scope,
    )
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="生成字段落标映射关系模板 (.xlsx)",
        start_detail="正在准备读取库表结构与已绑数据标准...",
        fn=StandardMigratorService(ctx).generate_binding_template_bytes,
        kwargs={
            "connection_id": connection_id,
            "schema_name": schema_name,
            "tables": tables,
            "schemas": schemas,
            "scope": scope,
        },
        finish_stage="模板已生成",
        default_detail="字段落标映射关系模板 (.xlsx) 生成完成",
        response_fn=lambda raw: file_download_response(
            raw,
            dl_name,
            media_type=MEDIA_TYPE_XLSX,
            ascii_fallback="binding_template.xlsx",
        ),
    )


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
    raw = await file.read()
    return await run_tracked_upload(
        ctx=ctx,
        progress_id=progress_id,
        title="解析表字段与数据标准对照关系",
        fn=StandardMigratorService(ctx).upload_field_bindings_excel,
        kwargs={
            "filename": file.filename or "bindings.xlsx",
            "content": raw,
            "connection_id": connection_id,
            "default_schema": default_schema,
            "mode": mode,
            "format_profile": format_profile,
        },
        default_detail="对照关系导入完成",
        count_fn=lambda r: (
            int(r.get("imported_count", 0)),
            int(r.get("imported_count", 0)),
        ),
    )


@router.get("/bindings/report.xlsx")
async def api_bindings_report(
    request: Request,
    connection_id: str = Query(default=""),
    format_profile: str = Query(default="AUTO"),
    tables: str = Query(default=""),
    schemas: str = Query(default=""),
    only_bound_tables: bool = Query(default=True),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="导出全库落标与贯标报告 (.xlsx)",
        start_detail="正在准备采集数据源目录并汇总全库落标与贯标数据...",
        fn=StandardMigratorService(ctx).export_binding_report_bytes,
        kwargs={
            "connection_id": connection_id,
            "format_profile": format_profile,
            "tables": tables,
            "schemas": schemas,
            "only_bound_tables": only_bound_tables,
        },
        finish_stage="导出完成",
        default_detail="单 Sheet 全库落标与贯标 Excel 报告已生成",
        response_fn=lambda raw: file_download_response(
            raw,
            "DataOG_全库落标与贯标报告.xlsx",
            media_type=MEDIA_TYPE_XLSX,
            ascii_fallback="binding_report.xlsx",
        ),
    )


@router.get("/bindings/report.html")
async def api_bindings_report_html(
    request: Request,
    connection_id: str = Query(default=""),
    format_profile: str = Query(default="AUTO"),
    tables: str = Query(default=""),
    schemas: str = Query(default=""),
    only_bound_tables: bool = Query(default=True),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="导出离线交互网页报告 (.html)",
        start_detail="正在准备采集数据源目录并构建离线可交互库级报告...",
        fn=StandardMigratorService(ctx).export_binding_report_html_bytes,
        kwargs={
            "connection_id": connection_id,
            "format_profile": format_profile,
            "tables": tables,
            "schemas": schemas,
            "only_bound_tables": only_bound_tables,
        },
        finish_stage="导出完成",
        default_detail="离线交互库级网页报告 (.html) 已生成，支持直接双击打开并导出 Excel",
        response_fn=lambda raw: file_download_response(
            raw,
            "DataOG_全库落标与贯标离线报告.html",
            media_type=MEDIA_TYPE_HTML,
            ascii_fallback="binding_report.html",
        ),
    )


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
        return json_error(str(exc), 400)
