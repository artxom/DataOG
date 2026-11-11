"""DataOG 平台底座公共 API 路由 (/api/platform)."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import JSONResponse

from core.platform.context import PlatformContext, get_platform_context
from core.platform.schemas import (
    ConnectionPayload,
    MetadataRefreshPayload,
    QueryExecutePayload,
    QueryExportPayload,
)

router = APIRouter(prefix="/api/platform", tags=["platform"])


def _err(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


@router.get("/bootstrap")
def api_platform_bootstrap(ctx: PlatformContext = Depends(get_platform_context)):
    return {"ok": True, **ctx.build_platform_bootstrap()}


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
def api_refresh_metadata_tree(
    connection_id: str,
    payload: Optional[MetadataRefreshPayload] = None,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        schema_name = payload.schema_name if payload else None
        tree = ctx.metadata_hub.refresh_catalog(
            connection_id, schema_name=schema_name
        )
        return {
            "ok": True,
            **tree,
            "message": f"成功刷新元数据缓存（共 {len(tree['schemas'])} 个模式、{len(tree['tables'])} 个对象）",
        }
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/metadata/{connection_id}/table")
def api_get_table_detail(
    connection_id: str,
    table_name: str = Query(...),
    schema_name: str = Query(default=""),
    force_refresh: bool = Query(default=False),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        meta = ctx.metadata_hub.get_table_metadata(
            connection_id,
            schema_name=schema_name,
            table_name=table_name,
            force_refresh=force_refresh,
            penetrate_views=True,
        )
        return {"ok": True, "table": meta.to_dict()}
    except Exception as exc:
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
def api_execute_query(
    payload: QueryExecutePayload,
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        result = ctx.query_engine.execute_sql(
            payload.connection_id,
            payload.sql,
            limit=payload.limit,
            timeout_sec=payload.timeout_sec,
        )
        return {"ok": True, "result": result.to_dict()}
    except Exception as exc:
        return _err(str(exc), 400)


@router.get("/query/preview")
def api_preview_table(
    connection_id: str = Query(...),
    table_name: str = Query(...),
    schema_name: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=2000),
    ctx: PlatformContext = Depends(get_platform_context),
):
    try:
        result = ctx.query_engine.preview_table(
            connection_id,
            schema_name=schema_name,
            table_name=table_name,
            limit=limit,
        )
        return {"ok": True, "result": result.to_dict()}
    except Exception as exc:
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


# =========================================================================
# 全局任务与 SQL 日志抽屉接口
# =========================================================================


@router.get("/logs")
def api_platform_logs(
    limit: int = Query(default=150, ge=1, le=1000),
    ctx: PlatformContext = Depends(get_platform_context),
):
    return {"ok": True, "logs": ctx.store.list_logs(limit=limit)}


@router.post("/logs/clear")
def api_platform_clear_logs(ctx: PlatformContext = Depends(get_platform_context)):
    ctx.store.clear_logs()
    return {"ok": True, "logs": []}
