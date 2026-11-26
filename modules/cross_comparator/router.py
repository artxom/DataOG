"""跨环境五虚拟列数标比对中心 API 路由 (/api/modules/cross_comparator)."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import (
    MEDIA_TYPE_HTML,
    MEDIA_TYPE_JSON,
    MEDIA_TYPE_XLSX,
    file_download_response,
    json_error,
    run_tracked_task,
    run_tracked_upload,
)
from modules.cross_comparator.schemas import (
    BindRoleConnectionPayload,
    PrefetchPairsPayload,
    PushToMigratorPayload,
    SaveCrossBindingsPayload,
    SaveCrossPairingsPayload,
)
from modules.cross_comparator.service import CrossComparatorService

router = APIRouter(prefix="/api/modules/cross_comparator", tags=["cross_comparator"])


@router.get("/overview")
async def api_cross_overview(
    request: Request,
    table_key: Optional[str] = Query(default=None),
    force_refresh: bool = Query(default=False),
    source_schema: Optional[str] = Query(default=None),
    source_table: Optional[str] = Query(default=None),
    target_schema: Optional[str] = Query(default=None),
    target_table: Optional[str] = Query(default=None),
    format_profile: str = Query(default="AUTO"),
    source_format_profile: str = Query(default="AUTO"),
    target_format_profile: str = Query(default="AUTO"),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="加载跨环境比对",
        start_detail="正在读取数据源连接与已保存映射表对...",
        fn=CrossComparatorService(ctx).get_overview_state,
        kwargs={
            "selected_table_key": table_key,
            "force_refresh": force_refresh,
            "source_schema": source_schema,
            "source_table": source_table,
            "target_schema": target_schema,
            "target_table": target_table,
            "format_profile": format_profile,
            "source_format_profile": source_format_profile,
            "target_format_profile": target_format_profile,
        },
        finish_stage="比对就绪",
        detail_fn=lambda r: (
            f"已就绪 [{r.get('active_table_key') or source_table or target_table}]（{len(((r.get('active_comparison') or {}).get('dual_rows') or []))} 条比对行）"
            if (r.get("active_table_key") or source_table or target_table)
            else f"已加载 {len(r.get('tables') or [])} 对已保存映射"
        ),
        count_fn=lambda r: (
            len(((r.get("active_comparison") or {}).get("dual_rows") or [])),
            len(((r.get("active_comparison") or {}).get("dual_rows") or [])),
        ),
    )


@router.get("/compare-pair")
async def api_cross_compare_pair(
    request: Request,
    table_key: Optional[str] = Query(default=None),
    std_table_key: Optional[str] = Query(default=None),
    force_refresh: bool = Query(default=False),
    source_schema: Optional[str] = Query(default=None),
    source_table: Optional[str] = Query(default=None),
    target_schema: Optional[str] = Query(default=None),
    target_table: Optional[str] = Query(default=None),
    format_profile: str = Query(default="AUTO"),
    source_format_profile: str = Query(default="AUTO"),
    target_format_profile: str = Query(default="AUTO"),
    source_connection_id: Optional[str] = Query(default=None),
    target_connection_id: Optional[str] = Query(default=None),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    eff_key = table_key if table_key is not None else std_table_key
    label = eff_key or target_table or source_table or "当前表对"
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="切换比对表对",
        start_detail=f"正在对比 [{label}] 元数据与数据标准...",
        fn=CrossComparatorService(ctx).compare_pair_lightweight,
        kwargs={
            "selected_table_key": eff_key,
            "force_refresh": force_refresh,
            "source_schema": source_schema,
            "source_table": source_table,
            "target_schema": target_schema,
            "target_table": target_table,
            "format_profile": format_profile,
            "source_format_profile": source_format_profile,
            "target_format_profile": target_format_profile,
            "source_connection_id": source_connection_id,
            "target_connection_id": target_connection_id,
        },
        finish_stage="比对就绪",
        detail_fn=lambda r: (
            f"已就绪 [{r.get('active_table_key') or target_table or source_table}]（{len(((r.get('active_comparison') or {}).get('dual_rows') or []))} 条比对行）"
            if (r.get("active_table_key") or target_table or source_table)
            else "比对表对就绪"
        ),
        count_fn=lambda r: (
            len(((r.get("active_comparison") or {}).get("dual_rows") or [])),
            len(((r.get("active_comparison") or {}).get("dual_rows") or [])),
        ),
    )


@router.get("/pairs/search")
def api_cross_search_pairs(
    keyword: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return CrossComparatorService(ctx).search_saved_pairs(keyword=keyword, limit=limit)
    except Exception as exc:
        return json_error(str(exc), 400)


@router.post("/prefetch-pairs")
async def api_cross_prefetch_pairs(
    request: Request,
    payload: Optional[PrefetchPairsPayload] = None,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    max_pairs = payload.max_pairs if payload else 60
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="预热元数据缓存",
        start_detail=f"正在批量预热前 {max_pairs} 对映射表结构...",
        fn=CrossComparatorService(ctx).prefetch_saved_pairs,
        kwargs={"max_pairs": max_pairs},
        finish_stage="预热完成",
        detail_fn=lambda r: (
            f"源端 {r.get('prefetched_source', 0)} 表 / 目标端 {r.get('prefetched_target', 0)} 表已就绪"
        ),
        warning_fn=lambda r: "；".join(r.get("warnings") or []),
        count_fn=lambda r: (
            int(r.get("prefetched_source") or 0) + int(r.get("prefetched_target") or 0),
            int(r.get("prefetched_source") or 0) + int(r.get("prefetched_target") or 0),
        ),
    )


@router.post("/bind-role")
async def api_cross_bind_role(
    payload: BindRoleConnectionPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    side_label = "来源库" if (payload.role or "").strip().lower() == "source" else "目标库"
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title=f"绑定{side_label}连接",
        start_detail=f"正在配置{side_label}数据源并刷新比对视图...",
        fn=CrossComparatorService(ctx).bind_role_connection,
        args=(payload.role, payload.connection_id),
        kwargs={
            "active_table_key": payload.active_table_key,
            "auto_refresh": payload.auto_refresh,
        },
        finish_stage="绑定完成",
        default_detail="数据源绑定完成",
    )


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
        return json_error(str(exc), 400)


@router.post("/standard/upload")
async def api_cross_upload_standard(
    file: UploadFile = File(...),
    progress_id: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    raw = await file.read()
    return await run_tracked_upload(
        ctx=ctx,
        progress_id=progress_id,
        title="解析跨环境标准与绑定配置",
        fn=CrossComparatorService(ctx).import_standard_or_bundle,
        args=(file.filename or "standard.xlsx", raw),
        default_detail="导入完成",
    )


@router.post("/standard/sync-from-migrator")
def api_cross_sync_from_migrator(ctx: PlatformContext = Depends(get_platform_context)):
    try:
        return CrossComparatorService(ctx).sync_standards_from_migrator()
    except Exception as exc:
        return json_error(str(exc), 400)


@router.post("/standard/clear")
def api_cross_clear_standards(ctx: PlatformContext = Depends(get_platform_context)):
    ctx.store.clear_cross_standards()
    return CrossComparatorService(ctx).get_overview_state()


@router.get("/bindings/export.json")
def api_cross_export_json(ctx: PlatformContext = Depends(get_platform_context)):
    bundle = ctx.store.export_cross_bindings_bundle()
    content = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
    return file_download_response(
        content,
        "DataOG_跨环境三方数标绑定配置包.json",
        media_type=MEDIA_TYPE_JSON,
        ascii_fallback="bindings.json",
    )


@router.get("/bindings/export.xlsx")
def api_cross_export_xlsx(
    table_key: Optional[str] = Query(default=None),
    ctx: PlatformContext = Depends(get_platform_context),
):
    content = CrossComparatorService(ctx).export_comparison_excel_bytes(
        std_table_key=table_key or None
    )
    return file_download_response(
        content,
        "DataOG_跨环境三栏数标比对与绑定报告.xlsx",
        media_type=MEDIA_TYPE_XLSX,
        ascii_fallback="cross_report.xlsx",
    )


@router.get("/report.xlsx")
async def api_cross_report_xlsx(
    request: Request,
    format_profile: str = Query(default="AUTO"),
    source_format_profile: str = Query(default="AUTO"),
    target_format_profile: str = Query(default="AUTO"),
    source_schemas: str = Query(default=""),
    target_schemas: str = Query(default=""),
    only_bound_tables: bool = Query(default=True),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="导出跨环境库级比对报告 (.xlsx)",
        start_detail="正在汇总已保存映射表对与推荐同名表，构建单 Sheet Excel...",
        fn=CrossComparatorService(ctx).export_cross_report_excel_bytes,
        kwargs={
            "format_profile": format_profile,
            "source_format_profile": source_format_profile,
            "target_format_profile": target_format_profile,
            "source_schemas": source_schemas,
            "target_schemas": target_schemas,
            "only_bound_tables": only_bound_tables,
        },
        finish_stage="导出完成",
        default_detail="跨环境库级比对报告 (.xlsx) 已生成",
        response_fn=lambda raw: file_download_response(
            raw,
            "DataOG_跨环境库级比对报告.xlsx",
            media_type=MEDIA_TYPE_XLSX,
            ascii_fallback="cross_db_report.xlsx",
        ),
    )


@router.get("/report.html")
async def api_cross_report_html(
    request: Request,
    format_profile: str = Query(default="AUTO"),
    source_format_profile: str = Query(default="AUTO"),
    target_format_profile: str = Query(default="AUTO"),
    source_schemas: str = Query(default=""),
    target_schemas: str = Query(default=""),
    only_bound_tables: bool = Query(default=True),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="导出跨环境离线网页报告 (.html)",
        start_detail="正在构建跨环境五虚拟列离线交互网页与推荐同名清单...",
        fn=CrossComparatorService(ctx).export_cross_report_html_bytes,
        kwargs={
            "format_profile": format_profile,
            "source_format_profile": source_format_profile,
            "target_format_profile": target_format_profile,
            "source_schemas": source_schemas,
            "target_schemas": target_schemas,
            "only_bound_tables": only_bound_tables,
        },
        finish_stage="导出完成",
        default_detail="跨环境离线交互网页报告 (.html) 已生成，支持离线查阅五栏视图与导出 Excel",
        response_fn=lambda raw: file_download_response(
            raw,
            "DataOG_跨环境库级比对离线报告.html",
            media_type=MEDIA_TYPE_HTML,
            ascii_fallback="cross_db_report.html",
        ),
    )


@router.post("/push-to-migrator")
async def api_cross_push_to_migrator(
    payload: PushToMigratorPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="推送贯标规则",
        start_detail="正在抽取目标库已绑标规则并推送至单库贯标执行台...",
        fn=CrossComparatorService(ctx).push_to_migrator,
        kwargs={
            "std_table_key": payload.std_table_key or None,
            "format_profile": payload.format_profile,
            "source_format_profile": payload.source_format_profile,
            "target_format_profile": payload.target_format_profile,
        },
        finish_stage="推送完成",
        detail_fn=lambda r: str(
            r.get("message") or f"已推送 {r.get('pushed_count', 0)} 条规则"
        ),
        count_fn=lambda r: (
            int(r.get("pushed_count") or 0),
            int(r.get("pushed_count") or 0),
        ),
    )


@router.post("/pairings/save")
async def api_cross_save_pairings(
    payload: SaveCrossPairingsPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return await run_tracked_task(
        request=request,
        task_id=task_id,
        ctx=ctx,
        title="保存跨环境映射",
        start_detail=f"正在持久化 {len(payload.pairings)} 条字段映射与数标绑定...",
        fn=CrossComparatorService(ctx).save_pairings,
        kwargs={
            "std_table_key": payload.std_table_key,
            "source_schema": payload.source_schema,
            "source_table": payload.source_table,
            "target_schema": payload.target_schema,
            "target_table": payload.target_table,
            "pairings": payload.pairings,
            "format_profile": payload.format_profile,
            "source_format_profile": payload.source_format_profile,
            "target_format_profile": payload.target_format_profile,
            "force_refresh": payload.force_refresh,
            "sync_to_field_bindings": payload.sync_to_field_bindings,
        },
        finish_stage="保存完成",
        default_detail="双列映射与书签绑定已保存",
        count_fn=lambda _r: (len(payload.pairings), len(payload.pairings)),
    )


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
        return file_download_response(
            content,
            "DataOG_跨环境字段映射铺底模板.xlsx",
            media_type=MEDIA_TYPE_XLSX,
            ascii_fallback="cross_pairings_template.xlsx",
        )
    except Exception as exc:
        return json_error(str(exc), 400)


@router.post("/pairings/upload")
async def api_cross_pairings_upload(
    file: UploadFile = File(...),
    format_profile: str = Form(default="AUTO"),
    source_format_profile: str = Form(default="AUTO"),
    target_format_profile: str = Form(default="AUTO"),
    progress_id: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    raw = await file.read()
    return await run_tracked_upload(
        ctx=ctx,
        progress_id=progress_id,
        title="解析跨环境映射铺底表",
        fn=CrossComparatorService(ctx).upload_pairings_excel,
        kwargs={
            "filename": file.filename or "cross_pairings.xlsx",
            "content": raw,
            "format_profile": format_profile,
            "source_format_profile": source_format_profile,
            "target_format_profile": target_format_profile,
        },
        default_detail="映射铺底完成",
        count_fn=lambda r: (
            int(r.get("imported_count", 0)),
            int(r.get("imported_count", 0)),
        ),
    )
