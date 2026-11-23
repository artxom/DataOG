"""平台 JDBC 驱动、数据源连接、元数据目录与通用只读查询子路由."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from core.platform.context import PlatformContext, get_platform_context
from core.platform.http_utils import extract_task_id, json_error as _err
from core.platform.schemas import (
    ConnectionPayload,
    ConnectionStandardFormatPayload,
    MetadataRefreshPayload,
    QueryExecutePayload,
    QueryExportPayload,
    ReadOnlyQueryExecutePayload,
)

router = APIRouter()

# =========================================================================
# 自定义 JDBC .jar 驱动管理中心
# =========================================================================


@router.get("/drivers")
def api_list_drivers(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "drivers": [d.to_dict() for d in ctx.store.list_jdbc_drivers()],
        "templates": ctx.driver_manager.list_templates(),
        "java_runtime": ctx.driver_manager.get_java_runtime_status(),
    }


@router.post("/drivers/upload")
async def api_upload_driver_jar(
    file: UploadFile = File(...),
    name: str = Form(default=""),
    db_type_hint: str = Form(default="CUSTOM_JDBC"),
    driver_class_hint: str = Form(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        raw_bytes = await file.read()
        pkg = ctx.driver_manager.register_uploaded_jar(
            filename=file.filename or "driver.jar",
            content=raw_bytes,
            name=name,
            db_type_hint=db_type_hint,
            driver_class_hint=driver_class_hint,
        )
        return {
            "ok": True,
            "driver": pkg.to_dict(),
            "drivers": [d.to_dict() for d in ctx.store.list_jdbc_drivers()],
            "message": f"成功上传并校验驱动包 [{pkg.name}] (SHA-256: {pkg.sha256[:16]}...)",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.delete("/drivers/{driver_id}")
def api_delete_driver_jar(
    driver_id: str, ctx: PlatformContext = Depends(get_platform_context)
):
    ok = ctx.driver_manager.delete_driver(driver_id)
    if not ok:
        return _err("未找到要删除的驱动档案", 404)
    return {
        "ok": True,
        "drivers": [d.to_dict() for d in ctx.store.list_jdbc_drivers()],
        "message": "驱动包已安全删除",
    }


# =========================================================================
# 全局命名数据源连接中心 (ConnectionHub)
# =========================================================================


@router.get("/connections")
def api_list_connections(ctx: PlatformContext = Depends(get_platform_context)):
    return {
        "ok": True,
        "connections": ctx.connection_hub.list_profiles(),
    }


@router.post("/connections")
def api_create_or_save_connection(
    payload: ConnectionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.connection_hub.save_profile(
            payload.model_dump(exclude_unset=True),
            connection_id=payload.connection_id,
        )
        tree = None
        warning = ""
        if payload.auto_refresh_metadata:
            try:
                tree = ctx.metadata_hub.refresh_catalog(saved.connection_id)
            except Exception as refresh_exc:
                warning = f"连接档案已保存，但自动采集元数据失败: {refresh_exc}"
        return {
            "ok": True,
            "connection": saved.to_public_dict(),
            "connections": ctx.connection_hub.list_profiles(),
            "catalog_tree": tree,
            "warning": warning,
            "message": f"数据源 [{saved.name}] 已保存",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.put("/connections/{connection_id}")
def api_update_connection(
    connection_id: str,
    payload: ConnectionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.connection_hub.save_profile(
            payload.model_dump(exclude_unset=True),
            connection_id=connection_id,
        )
        tree = None
        if payload.auto_refresh_metadata:
            tree = ctx.metadata_hub.refresh_catalog(saved.connection_id)
        return {
            "ok": True,
            "connection": saved.to_public_dict(),
            "connections": ctx.connection_hub.list_profiles(),
            "catalog_tree": tree,
            "message": f"数据源 [{saved.name}] 已更新",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.put("/connections/{connection_id}/standard-format")
def api_update_connection_standard_format(
    connection_id: str,
    payload: ConnectionStandardFormatPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        saved = ctx.connection_hub.update_standard_format(
            connection_id, payload.standard_format
        )
        return {
            "ok": True,
            "connection": saved.to_public_dict(),
            "connections": ctx.connection_hub.list_profiles(),
            "message": f"数据源 [{saved.name}] 的落标标准格式已保存为 [{saved.standard_format}]",
        }
    except LookupError as exc:
        return _err(str(exc), 404)
    except Exception as exc:
        return _err(str(exc), 400)


@router.delete("/connections/{connection_id}")
def api_delete_connection(
    connection_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    ok = ctx.connection_hub.delete_profile(connection_id)
    if not ok:
        return _err("未找到指定的数据源连接档案", 404)
    return {
        "ok": True,
        "connections": ctx.connection_hub.list_profiles(),
        "message": "数据源连接档案已删除",
    }


@router.post("/connections/test")
def api_test_unsaved_connection(
    payload: ConnectionPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        profile = ctx.connection_hub.build_profile_from_payload(
            payload.model_dump(exclude_unset=True),
            connection_id=payload.connection_id,
        )
        res = ctx.connection_hub.test_profile(profile)
        return res
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/connections/{connection_id}/test")
def api_test_saved_connection(
    connection_id: str,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        return ctx.connection_hub.test_connection_by_id(connection_id)
    except Exception as exc:
        return _err(str(exc), 400)


# =========================================================================
# 全局持久化元数据中枢 (MetadataHub)
# =========================================================================


@router.get("/metadata/{connection_id}/tree")
def api_get_metadata_tree(
    connection_id: str,
    auto_refresh: bool = Query(default=False),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        tree = ctx.metadata_hub.get_catalog_tree(
            connection_id, auto_refresh=auto_refresh
        )
        return {"ok": True, **tree}
    except Exception as exc:
        return _err(str(exc), 400)


@router.post("/metadata/{connection_id}/refresh")
async def api_refresh_metadata_tree(
    connection_id: str,
    request: Request,
    payload: Optional[MetadataRefreshPayload] = None,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        schema_name = payload.schema_name if payload else None
        if tid:
            ctx.task_progress.start(
                tid,
                "刷新数据源目录缓存",
                detail=f"准备扫描 {schema_name or '全部模式'} 表与视图清单...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        tree = await run_in_threadpool(
            ctx.metadata_hub.refresh_catalog,
            connection_id,
            schema_name=schema_name,
            progress_cb=reporter,
        )
        msg = f"成功刷新元数据缓存（共 {len(tree['schemas'])} 个模式、{len(tree['tables'])} 个对象）"
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="目录刷新完成",
                detail=msg,
                processed=len(tree["tables"]),
                total=len(tree["tables"]),
            )
        return {
            "ok": True,
            **tree,
            "message": msg,
        }
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.get("/metadata/{connection_id}/table")
async def api_get_table_detail(
    connection_id: str,
    request: Request,
    table_name: str = Query(...),
    schema_name: str = Query(default=""),
    force_refresh: bool = Query(default=False),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        target_label = f"{schema_name}.{table_name}" if schema_name else table_name
        if tid:
            ctx.task_progress.start(
                tid,
                "加载库表元数据",
                detail=f"正在读取 {target_label} 表结构...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        meta = await run_in_threadpool(
            ctx.metadata_hub.get_table_metadata,
            connection_id,
            schema_name=schema_name,
            table_name=table_name,
            force_refresh=force_refresh,
            penetrate_views=True,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            detail_msg = (
                f"已加载 {meta.schema_name}.{meta.table_name}（共 {len(meta.columns)} 列）"
                if meta.exists
                else f"对象 {meta.schema_name}.{meta.table_name} 不存在"
            )
            ctx.task_progress.finish(
                tid,
                stage="结构加载完成",
                detail=detail_msg,
                processed=len(meta.columns),
                total=len(meta.columns),
            )
        return {"ok": True, "table": meta.to_dict()}
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.get("/metadata/search")
def api_search_metadata(
    keyword: str = Query(default=""),
    connection_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    ctx: PlatformContext = Depends(get_platform_context),
):
    hits = ctx.metadata_hub.search_objects(
        keyword=keyword, connection_id=connection_id or None, limit=limit
    )
    return {"ok": True, "hits": hits}


# =========================================================================
# 通用简单查询与表数据预览中心 (QueryEngine)
# =========================================================================


@router.post("/query/execute")
async def api_execute_query(
    payload: QueryExecutePayload,
    request: Request,
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        if tid:
            ctx.task_progress.start(
                tid,
                "执行 SQL 查询",
                detail=f"正在准备执行 SQL（上限 {payload.limit} 行）...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        result = await run_in_threadpool(
            ctx.query_engine.execute_sql,
            payload.connection_id,
            payload.sql,
            limit=payload.limit,
            timeout_sec=payload.timeout_sec,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="查询完成",
                detail=result.message or f"返回 {result.row_count} 行",
                processed=result.row_count,
                total=result.row_count,
            )
        return {"ok": True, "result": result.to_dict()}
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.post("/query/execute-readonly")
def api_execute_readonly_query(
    payload: ReadOnlyQueryExecutePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        result = ctx.query_engine.execute_readonly_sql(
            payload.connection_id,
            payload.sql,
            limit=payload.limit,
            max_limit_cap=200,
            timeout_sec=payload.timeout_sec,
        )
        return {"ok": True, "result": result.to_dict()}
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/query/preview")
async def api_preview_table(
    request: Request,
    connection_id: str = Query(...),
    table_name: str = Query(...),
    schema_name: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=2000),
    task_id: str = Query(default=""),
    ctx: PlatformContext = Depends(get_platform_context),
):
    tid = extract_task_id(request, task_id)
    try:
        target_label = f"{schema_name}.{table_name}" if schema_name else table_name
        if tid:
            ctx.task_progress.start(
                tid,
                "预览表数据",
                detail=f"正在采样 {target_label}（最多 {limit} 行）...",
            )
        reporter = ctx.task_progress.make_reporter(tid)
        result = await run_in_threadpool(
            ctx.query_engine.preview_table,
            connection_id,
            schema_name=schema_name,
            table_name=table_name,
            limit=limit,
            progress_cb=reporter,
        )
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.finish(
                tid,
                stage="数据预览完成",
                detail=result.message or f"返回 {result.row_count} 行",
                processed=result.row_count,
                total=result.row_count,
            )
        return {"ok": True, "result": result.to_dict()}
    except Exception as exc:
        if tid and not ctx.task_progress.is_cancelled(tid):
            ctx.task_progress.fail(tid, str(exc))
        return _err(str(exc), 400)


@router.post("/query/export")
def api_export_query_result(
    payload: QueryExportPayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        columns = list(payload.columns)
        rows = list(payload.rows)
        if not columns and payload.connection_id and payload.sql:
            q_res = ctx.query_engine.execute_sql(
                payload.connection_id, payload.sql, limit=payload.limit
            )
            columns = [c.name for c in q_res.columns]
            rows = q_res.rows

        if not columns:
            return _err("没有可导出的查询结果列", 400)

        content, media_type = ctx.query_engine.export_result_bytes(
            columns=columns,
            rows=rows,
            fmt=payload.format,
            title=payload.title,
        )
        ext = "xlsx" if payload.format.strip().lower() in ("xlsx", "excel") else "csv"
        safe_fn = quote(f"{payload.title or 'query_result'}.{ext}")
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename={ext}_export.{ext}; filename*=UTF-8''{safe_fn}"
            },
        )
    except Exception as exc:
        return _err(str(exc), 400)


