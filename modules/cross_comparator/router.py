"""16:9 跨环境三栏数标比对中心 API 路由 (/api/modules/cross_comparator)."""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.router import extract_task_id
from modules.cross_comparator.schemas import (
    BindRoleConnectionPayload,
    PrefetchPairsPayload,
    PushToMigratorPayload,
    SaveCrossBindingsPayload,
    SaveCrossPairingsPayload,
)
from modules.cross_comparator.service import CrossComparatorService

router = APIRouter(prefix="/api/modules/cross_comparator", tags=["cross_comparator"])


def _err(msg: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status_code)


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
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "加载跨环境比对",
                detail="正在读取数据源连接与已保存映射表对...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        res = await run_in_threadpool(
            CrossComparatorService(ctx).get_overview_state,
            selected_table_key=table_key,
            force_refresh=force_refresh,
            source_schema=source_schema,
            source_table=source_table,
            target_schema=target_schema,
            target_table=target_table,
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            active_tbl = res.get("active_table_key") or source_table or target_table or ""
            comp = res.get("active_comparison") or {}
            dual_cnt = len(comp.get("dual_rows") or [])
            detail_msg = (
                f"已就绪 [{active_tbl}]（{dual_cnt} 条比对行）"
                if active_tbl
                else f"已加载 {len(res.get('tables') or [])} 对已保存映射"
            )
            ctx.task_progress.finish(
                tid,
                stage="比对就绪",
                detail=detail_msg,
                warning=str(res.get("warning") or ""),
                processed=dual_cnt,
                total=dual_cnt,
            )
        return res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


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
    tid = extract_task_id(request, task_id)
    eff_key = table_key if table_key is not None else std_table_key
    try:
        if tid:
            label = eff_key or target_table or source_table or "当前表对"
            ctx.task_progress.start(
                tid,
                "切换比对表对",
                detail=f"正在对比 [{label}] 元数据与数据标准...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        res = await run_in_threadpool(
            CrossComparatorService(ctx).compare_pair_lightweight,
            selected_table_key=eff_key,
            force_refresh=force_refresh,
            source_schema=source_schema,
            source_table=source_table,
            target_schema=target_schema,
            target_table=target_table,
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
            source_connection_id=source_connection_id,
            target_connection_id=target_connection_id,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            active_tbl = res.get("active_table_key") or target_table or source_table or ""
            comp = res.get("active_comparison") or {}
            dual_cnt = len(comp.get("dual_rows") or [])
            ctx.task_progress.finish(
                tid,
                stage="比对就绪",
                detail=f"已就绪 [{active_tbl}]（{dual_cnt} 条比对行）" if active_tbl else "比对表对就绪",
                warning=str(res.get("warning") or ""),
                processed=dual_cnt,
                total=dual_cnt,
            )
        return res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.get("/pairs/search")
def api_cross_search_pairs(
    keyword: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return CrossComparatorService(ctx).search_saved_pairs(keyword=keyword, limit=limit)
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/prefetch-pairs")
async def api_cross_prefetch_pairs(
    request: Request,
    payload: Optional[PrefetchPairsPayload] = None,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    max_pairs = payload.max_pairs if payload else 60
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "预热元数据缓存",
                detail=f"正在批量预热前 {max_pairs} 对映射表结构...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        res = await run_in_threadpool(
            CrossComparatorService(ctx).prefetch_saved_pairs,
            max_pairs=max_pairs,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            warn_text = "；".join(res.get("warnings") or [])
            total_pre = int(res.get("prefetched_source") or 0) + int(
                res.get("prefetched_target") or 0
            )
            ctx.task_progress.finish(
                tid,
                stage="预热完成",
                detail=f"源端 {res.get('prefetched_source', 0)} 表 / 目标端 {res.get('prefetched_target', 0)} 表已就绪",
                warning=warn_text,
                processed=total_pre,
                total=total_pre,
            )
        return res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.post("/bind-role")
async def api_cross_bind_role(
    payload: BindRoleConnectionPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            side_label = "来源库" if (payload.role or "").strip().lower() == "source" else "目标库"
            ctx.task_progress.start(
                tid,
                f"绑定{side_label}连接",
                detail=f"正在配置{side_label}数据源并刷新比对视图...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        res = await run_in_threadpool(
            CrossComparatorService(ctx).bind_role_connection,
            payload.role,
            payload.connection_id,
            active_table_key=payload.active_table_key,
            auto_refresh=payload.auto_refresh,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="绑定完成",
                detail=str(res.get("message") or "数据源绑定完成"),
                warning=str(res.get("warning") or ""),
            )
        return res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
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


@router.get("/report.xlsx")
async def api_cross_report_xlsx(
    request: Request,
    format_profile: str = Query(default="AUTO"),
    source_format_profile: str = Query(default="AUTO"),
    target_format_profile: str = Query(default="AUTO"),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "导出跨环境库级比对报告 (.xlsx)",
                detail="正在汇总已保存映射表对与推荐同名表，构建单 Sheet Excel...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        content = await run_in_threadpool(
            CrossComparatorService(ctx).export_cross_report_excel_bytes,
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="导出完成",
                detail="跨环境库级比对报告 (.xlsx) 已生成",
            )
        fn = quote("DataOG_跨环境库级比对报告.xlsx")
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename=cross_db_report.xlsx; filename*=UTF-8''{fn}"
            },
        )
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.get("/report.html")
async def api_cross_report_html(
    request: Request,
    format_profile: str = Query(default="AUTO"),
    source_format_profile: str = Query(default="AUTO"),
    target_format_profile: str = Query(default="AUTO"),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "导出跨环境离线网页报告 (.html)",
                detail="正在构建跨环境五虚拟列离线交互网页与推荐同名清单...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        content = await run_in_threadpool(
            CrossComparatorService(ctx).export_cross_report_html_bytes,
            format_profile=format_profile,
            source_format_profile=source_format_profile,
            target_format_profile=target_format_profile,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="导出完成",
                detail="跨环境离线交互网页报告 (.html) 已生成，支持离线查阅五栏视图与导出 Excel",
            )
        fn = quote("DataOG_跨环境库级比对离线报告.html")
        return Response(
            content=content,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=cross_db_report.html; filename*=UTF-8''{fn}",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.post("/push-to-migrator")
async def api_cross_push_to_migrator(
    payload: PushToMigratorPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "推送贯标规则",
                detail="正在抽取目标库已绑标规则并推送至单库贯标执行台...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        res = await run_in_threadpool(
            CrossComparatorService(ctx).push_to_migrator,
            std_table_key=payload.std_table_key or None,
            format_profile=payload.format_profile,
            source_format_profile=payload.source_format_profile,
            target_format_profile=payload.target_format_profile,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="推送完成",
                detail=str(res.get("message") or f"已推送 {res.get('pushed_count', 0)} 条规则"),
                warning=str(res.get("warning") or ""),
                processed=int(res.get("pushed_count") or 0),
                total=int(res.get("pushed_count") or 0),
            )
        return res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.post("/pairings/save")
async def api_cross_save_pairings(
    payload: SaveCrossPairingsPayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "保存跨环境映射",
                detail=f"正在持久化 {len(payload.pairings)} 条字段映射与数标绑定...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        res = await run_in_threadpool(
            CrossComparatorService(ctx).save_pairings,
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
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="保存完成",
                detail=str(res.get("message") or "双列映射与书签绑定已保存"),
                warning=str(res.get("warning") or ""),
                processed=len(payload.pairings),
                total=len(payload.pairings),
            )
        return res
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
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
